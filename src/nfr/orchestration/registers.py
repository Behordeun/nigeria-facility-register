"""
Published-register history: keep the last few registers, restore one, and hold
a rolled-back vintage so it is not published again.

Every publish first hard-links the outgoing register into the archive folder
(no copy, and the live path is never missing, because the new register then
replaces it with one atomic rename). The archive keeps the newest few. A restore
copies an archived register back onto the live path with the same atomic
rename, archives the register it replaces so the restore can itself be undone,
and writes a hold naming the replaced register's fingerprint: while the inputs
still fingerprint the same, a refresh would only rebuild the release that was
just rolled back, so the refresh refuses to publish it. The hold lifts when the
inputs change (a fixed source gives a new fingerprint), when it is released by
hand, or when a different register is published.

Pure file operations over injected paths, so it is tested without Dagster.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from nfr.orchestration import changes, publish

ARCHIVE_SUFFIX = ".duckdb"


class RestoreError(RuntimeError):
    """A restore was refused; the message says why and what to do instead."""


def _fingerprint_of(db: Path) -> str | None:
    """The fingerprint a register records, or None when it records none or the
    file cannot be read as a register; keeping a file must never fail on that."""
    try:
        return publish.read_published_fingerprint(str(db))
    except duckdb.Error:
        return None


def _archive_target(archive_dir: Path, db: Path, now: datetime) -> Path:
    """Timestamp first, so names sort oldest to newest; the fingerprint after it
    says which inputs the register was built from. A counter keeps two archives
    made in the same microsecond apart rather than overwriting one."""
    fingerprint = _fingerprint_of(db) or "unknown"
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = archive_dir / f"{stamp}-{fingerprint}{ARCHIVE_SUFFIX}"
    n = 1
    while target.exists():
        target = archive_dir / f"{stamp}.{n}-{fingerprint}{ARCHIVE_SUFFIX}"
        n += 1
    return target


def archive_live(db: Path, archive_dir: Path, now: datetime | None = None) -> Path | None:
    """Keep the register currently at db in archive_dir; return its archive path.

    A hard link, so nothing is copied and db stays in place for readers until
    the caller atomically replaces it. Falls back to a copy where the filesystem
    has no hard links. Returns None when nothing is published yet.
    """
    if not db.is_file():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = _archive_target(archive_dir, db, now or datetime.now(timezone.utc))
    try:
        os.link(db, target)
    except OSError:
        shutil.copy2(db, target)
    return target


def archived(archive_dir: Path) -> list[Path]:
    """Archived registers, oldest first."""
    if not archive_dir.is_dir():
        return []
    return sorted(p for p in archive_dir.iterdir() if p.is_file() and p.name.endswith(ARCHIVE_SUFFIX))


def prune(archive_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` archived registers; return what was removed."""
    removed = archived(archive_dir)[:-keep] if keep > 0 else archived(archive_dir)
    for path in removed:
        path.unlink(missing_ok=True)
    return removed


def write_hold(hold_file: Path, fingerprint: str, reason: str) -> None:
    """Record that the register built from `fingerprint` was rolled back."""
    hold_file.parent.mkdir(parents=True, exist_ok=True)
    hold_file.write_text(json.dumps({"fingerprint": fingerprint, "reason": reason}) + "\n")


def read_hold(hold_file: Path) -> str | None:
    """The held fingerprint, or None when nothing is held.

    An unreadable hold file is treated as a hold on nothing rather than raising,
    so a damaged file can never stop the sensor; release it by hand to clear it.
    """
    if not hold_file.is_file():
        return None
    try:
        held = json.loads(hold_file.read_text()).get("fingerprint")
    except (OSError, ValueError, AttributeError):
        return None
    return held if isinstance(held, str) and held else None


def clear_hold(hold_file: Path) -> bool:
    """Release the hold; return whether there was one."""
    if hold_file.is_file():
        hold_file.unlink()
        return True
    return False


def restore(
    archive_file: Path | str,
    db: Path,
    archive_dir: Path,
    hold_file: Path,
    keep: int,
    staging_dir: Path | None = None,
    force: bool = False,
) -> str | None:
    """Put an archived register back on the live path; return the held fingerprint.

    Refuses a file outside archive_dir, and refuses while a refresh appears to
    be building (a side database in staging_dir) unless forced, because a run
    that finishes after the restore would publish over it. The register being
    replaced is archived first and its fingerprint held, so the restore can be
    undone and the rolled-back release is not rebuilt straight away.
    """
    archive_dir = archive_dir.resolve()
    archive_file = Path(archive_file)
    source = archive_file if archive_file.is_absolute() else archive_dir / archive_file
    source = source.resolve()
    if source.parent != archive_dir or not source.is_file():
        raise RestoreError(f"{archive_file} is not an archived register in {archive_dir}; list them first")
    building = [p.name for p in staging_dir.iterdir()] if staging_dir and staging_dir.is_dir() else []
    if building and not force:
        raise RestoreError(
            f"a refresh appears to be building ({', '.join(building)} in {staging_dir}); wait for it to finish, "
            "or pass --force if that file is left over from a crashed run"
        )

    replaced = _fingerprint_of(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    kept: Path | None = None
    tmp: Path | None = None
    try:
        kept = archive_live(db, archive_dir)
        fd, tmp_name = tempfile.mkstemp(dir=str(db.parent), prefix=f".{db.name}.", suffix=".restore")
        os.close(fd)
        tmp = Path(tmp_name)
        shutil.copy2(source, tmp)
        # The copy, not the archive, gets a change record if it predates them,
        # so the dashboard's change table never goes missing after a rollback.
        changes.ensure_record(tmp, "restored from an archive made before change records existed")
        os.replace(tmp, db)
    except BaseException:
        # The live register is unchanged, so the copy of it kept a moment ago
        # is a duplicate; drop it rather than leave an extra archive entry.
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        if kept is not None:
            kept.unlink(missing_ok=True)
        raise
    restored = _fingerprint_of(db)
    if replaced and replaced != restored:
        write_hold(hold_file, replaced, f"rolled back to {source.name}")
    with contextlib.suppress(OSError):
        prune(archive_dir, keep)
    return replaced if replaced != restored else None
