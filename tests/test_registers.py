"""Published-register history: archive, prune, restore and the rollback hold.

A bad register that passes the quality gate must be recoverable: every publish
keeps the outgoing register, a restore puts one back atomically and can itself
be undone, and the rolled-back vintage is held so no refresh republishes it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from nfr.orchestration import publish, registers


def _register(path, fingerprint, marker):
    """A DuckDB register carrying a published fingerprint and a marker row."""
    con = duckdb.connect(str(path))
    con.execute("create table marker (v varchar)")
    con.execute("insert into marker values (?)", [marker])
    con.close()
    publish.write_published_fingerprint(str(path), fingerprint)
    return path


def _marker(path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute("select v from marker").fetchone()[0]
    finally:
        con.close()


def _at(second):
    return datetime(2026, 11, 9, 8, 0, second, tzinfo=timezone.utc)


def test_archive_keeps_the_live_register_in_place(tmp_path):
    live = _register(tmp_path / "nfr.duckdb", "fpA", "A")
    archive = tmp_path / "published"
    kept = registers.archive_live(live, archive, _at(1))
    assert live.is_file(), "archiving must not remove the live register"
    assert kept.name.endswith("-fpA.duckdb"), "the archive name records the fingerprint"
    assert kept.stat().st_ino == live.stat().st_ino, "a hard link, not a copy"


def test_archive_is_a_no_op_before_anything_is_published(tmp_path):
    assert registers.archive_live(tmp_path / "absent.duckdb", tmp_path / "published") is None


def test_prune_keeps_the_newest(tmp_path):
    archive = tmp_path / "published"
    live = tmp_path / "nfr.duckdb"
    for second, fp in enumerate(["fp1", "fp2", "fp3", "fp4", "fp5"], start=1):
        live.unlink(missing_ok=True)
        _register(live, fp, fp)
        registers.archive_live(live, archive, _at(second))
    removed = registers.prune(archive, keep=3)
    names = [p.name for p in registers.archived(archive)]
    assert len(removed) == 2
    assert [n.split("-")[1] for n in names] == ["fp3.duckdb", "fp4.duckdb", "fp5.duckdb"], names


def test_restore_puts_the_archived_register_back_and_holds_the_replaced_one(tmp_path):
    archive = tmp_path / "published"
    live = _register(tmp_path / "nfr.duckdb", "good", "good")
    good = registers.archive_live(live, archive, _at(1))
    live.unlink()
    _register(live, "bad", "bad")
    hold = tmp_path / "hold.json"

    held = registers.restore(good, live, archive, hold, keep=3)

    assert _marker(live) == "good", "the archived register must be live again"
    assert publish.read_published_fingerprint(str(live)) == "good"
    assert held == "bad"
    assert registers.read_hold(hold) == "bad", "the rolled-back vintage must be held"
    archived_fps = [p.name.rsplit("-", 1)[1] for p in registers.archived(archive)]
    assert "bad.duckdb" in archived_fps, "the replaced register is archived, so the restore can be undone"
    assert good.is_file(), "a restore copies, so the same register can be restored again"


def test_restoring_the_live_vintage_sets_no_hold(tmp_path):
    archive = tmp_path / "published"
    live = _register(tmp_path / "nfr.duckdb", "same", "same")
    kept = registers.archive_live(live, archive, _at(1))
    hold = tmp_path / "hold.json"
    assert registers.restore(kept, live, archive, hold, keep=3) is None
    assert registers.read_hold(hold) is None


def test_restore_accepts_a_bare_archive_name(tmp_path):
    archive = tmp_path / "published"
    live = _register(tmp_path / "nfr.duckdb", "good", "good")
    good = registers.archive_live(live, archive, _at(1))
    live.unlink()
    _register(live, "bad", "bad")
    registers.restore(good.name, live, archive, tmp_path / "hold.json", keep=3)
    assert _marker(live) == "good"


def test_restore_refuses_a_file_outside_the_archive(tmp_path):
    archive = tmp_path / "published"
    archive.mkdir()
    live = _register(tmp_path / "nfr.duckdb", "live", "live")
    stray = _register(tmp_path / "stray.duckdb", "stray", "stray")
    with pytest.raises(registers.RestoreError, match="not an archived register"):
        registers.restore(stray, live, archive, tmp_path / "hold.json", keep=3)
    assert _marker(live) == "live", "a refused restore must leave the live register alone"


def test_restore_refuses_while_a_refresh_is_building_unless_forced(tmp_path):
    archive = tmp_path / "published"
    live = _register(tmp_path / "nfr.duckdb", "good", "good")
    good = registers.archive_live(live, archive, _at(1))
    live.unlink()
    _register(live, "bad", "bad")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "nfr_run.duckdb").write_bytes(b"in progress")
    hold = tmp_path / "hold.json"
    with pytest.raises(registers.RestoreError, match="appears to be building"):
        registers.restore(good, live, archive, hold, keep=3, staging_dir=staging)
    assert _marker(live) == "bad", "a refused restore must leave the live register alone"
    registers.restore(good, live, archive, hold, keep=3, staging_dir=staging, force=True)
    assert _marker(live) == "good"


def test_hold_round_trips_and_clears(tmp_path):
    hold = tmp_path / "hold.json"
    assert registers.read_hold(hold) is None
    registers.write_hold(hold, "fpX", "test")
    assert registers.read_hold(hold) == "fpX"
    assert registers.clear_hold(hold) is True
    assert registers.clear_hold(hold) is False
    assert registers.read_hold(hold) is None


@pytest.mark.parametrize("content", ["not json", "[]", '{"fingerprint": ""}', '{"other": 1}'])
def test_an_unreadable_hold_holds_nothing(tmp_path, content):
    # A damaged hold file must never stop the sensor; it is released by hand.
    hold = tmp_path / "hold.json"
    hold.write_text(content)
    assert registers.read_hold(hold) is None


def test_a_failed_restore_leaves_the_live_register_and_the_archive_as_they_were(tmp_path, monkeypatch):
    archive = tmp_path / "published"
    live = _register(tmp_path / "nfr.duckdb", "good", "good")
    good = registers.archive_live(live, archive, _at(1))
    live.unlink()
    _register(live, "bad", "bad")
    before = sorted(p.name for p in registers.archived(archive))

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(registers.shutil, "copy2", fail)
    with pytest.raises(OSError, match="disk full"):
        registers.restore(good, live, archive, tmp_path / "hold.json", keep=3)
    assert _marker(live) == "bad", "a failed restore must leave the live register alone"
    assert sorted(p.name for p in registers.archived(archive)) == before, "no extra archive entry is left behind"
    assert registers.read_hold(tmp_path / "hold.json") is None


def test_a_restore_that_cannot_stage_its_copy_leaves_the_archive_as_it_was(tmp_path, monkeypatch):
    archive = tmp_path / "published"
    live = _register(tmp_path / "nfr.duckdb", "good", "good")
    good = registers.archive_live(live, archive, _at(1))
    live.unlink()
    _register(live, "bad", "bad")
    before = sorted(p.name for p in registers.archived(archive))

    def fail(*_args, **_kwargs):
        raise OSError("no space for the staging file")

    monkeypatch.setattr(registers.tempfile, "mkstemp", fail)
    with pytest.raises(OSError, match="no space for the staging file"):
        registers.restore(good, live, archive, tmp_path / "hold.json", keep=3)
    assert _marker(live) == "bad", "a restore that cannot stage its copy must leave the live register alone"
    assert sorted(p.name for p in registers.archived(archive)) == before, "the archive entry made for the live register is removed"
    assert registers.read_hold(tmp_path / "hold.json") is None


def test_restore_creates_the_live_folder_when_nothing_is_published(tmp_path):
    archive = tmp_path / "published"
    archive.mkdir()
    source = _register(archive / "20261109T080000000000Z-good.duckdb", "good", "good")
    live = tmp_path / "fresh" / "nfr.duckdb"
    assert registers.restore(source.name, live, archive, tmp_path / "hold.json", keep=3) is None
    assert _marker(live) == "good"
