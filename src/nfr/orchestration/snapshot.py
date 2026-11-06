"""
Immutable per-run snapshot of the raw inputs.

The sensor fingerprints the live data/raw/ directory and launches a run for that
fingerprint, but the run reads the same mutable directory later, so between the
two the files can change (a new --place, a hand edit). Without a snapshot a run
could load different bytes than the fingerprint it was launched for, and publish
them under that fingerprint, or load a pair that changed mid-run.

At the start of a run, copy the fingerprinted files into a run-scoped snapshot
directory and load from there, never from the live directory. Each file is
copied through a same-directory temp name and atomically renamed, so a reader of
the snapshot never sees a half-copied file, and once copied the snapshot is the
run's fixed, private view of the inputs: later changes to data/raw/ cannot alter
what this run builds. The caller verifies the snapshot's fingerprint against the
one the run was launched for before using it, so a directory that changed
between launch and snapshot is caught rather than silently built.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def create_snapshot(source_dir: Path, dest_dir: Path, names: list[str]) -> Path:
    """Copy each named file from source_dir into dest_dir atomically.

    dest_dir is created fresh (any stale content from a crashed run with the
    same id is cleared first). Every name must exist in source_dir; a missing
    input raises before any copy, so a snapshot is all-or-nothing. Returns
    dest_dir.
    """
    source_dir = source_dir.resolve()
    dest_dir = dest_dir.resolve()

    if missing := [n for n in names if not (source_dir / n).is_file()]:
        raise FileNotFoundError(f"cannot snapshot missing inputs: {', '.join(missing)}")

    # Reject a source that resolves outside source_dir: a name in the raw
    # directory that is a symlink to a readable geopackage elsewhere on the host
    # would otherwise be copied into the snapshot and published as if it were a
    # project input. Resolve each source and confirm it stays within source_dir
    # before any copy, so the snapshot only ever holds real raw inputs.
    for name in names:
        real = (source_dir / name).resolve()
        if real != source_dir / name and source_dir not in real.parents:
            raise ValueError(f"raw input {name} resolves outside {source_dir}: {real}")

    # Start from a clean snapshot dir so a leftover file from a crashed earlier
    # run with this id cannot leak into this one.
    if dest_dir.exists():
        for existing in dest_dir.iterdir():
            if existing.is_file():
                existing.unlink()
    dest_dir.mkdir(parents=True, exist_ok=True)

    for name in names:
        src = source_dir / name
        final = dest_dir / name
        fd, tmp_name = tempfile.mkstemp(dir=str(dest_dir), prefix=f".{name}.", suffix=".part")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            tmp.write_bytes(src.read_bytes())
            os.replace(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return dest_dir


def clear_snapshot(dest_dir: Path) -> None:
    """Remove a run's snapshot directory and its contents, best-effort.

    Never raises: a missing directory is fine, and an unlink/rmdir error is
    swallowed. This matters after a successful promote, where the data is
    already live and the published sentinel has advanced; letting a cleanup
    error propagate there would mark a successful publish as failed and, because
    the sentinel moved, the sensor would not retry it. A leftover snapshot is a
    disk-space nuisance, not a correctness problem, so cleanup is advisory.
    """
    dest_dir = Path(dest_dir)
    if not dest_dir.exists():
        return
    for existing in dest_dir.iterdir():
        with contextlib.suppress(OSError):
            if existing.is_file():
                existing.unlink()
    with contextlib.suppress(OSError):
        # Non-empty or not removable; leave it rather than force a recursive
        # delete. A later run with the same id starts clean via create_snapshot.
        dest_dir.rmdir()
