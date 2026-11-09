"""Tests for the per-run immutable raw snapshot.

The snapshot gives a run a fixed, private copy of the raw inputs so a later
change to data/raw/ cannot alter what the run builds. These exercise the copy,
the missing-input guard, the clean-start behavior, and cleanup, plus that the
snapshot's fingerprint matches the source so the tag-verification in the asset
is meaningful. No Dagster context is needed; duckdb/dagster are base deps but
nothing here imports them, so this runs everywhere.
"""
from __future__ import annotations

import pytest

from nfr.orchestration import snapshot

NAMES = ["a.gpkg", "b.gpkg"]


def _seed(src, contents: dict[str, bytes]):
    src.mkdir(parents=True, exist_ok=True)
    for name, data in contents.items():
        (src / name).write_bytes(data)


def test_snapshot_copies_every_named_file(tmp_path):
    src = tmp_path / "raw"
    _seed(src, {"a.gpkg": b"AAAA", "b.gpkg": b"BBBB"})
    dest = snapshot.create_snapshot(src, tmp_path / "snap", NAMES)
    assert (dest / "a.gpkg").read_bytes() == b"AAAA"
    assert (dest / "b.gpkg").read_bytes() == b"BBBB"
    # No staging temp files are left behind.
    assert not list(dest.glob(".*.part"))


def test_snapshot_is_independent_of_later_source_changes(tmp_path):
    # The whole point: once snapshotted, changing the source does not change the
    # snapshot, so a run built from the snapshot is unaffected by a data/raw/ edit.
    src = tmp_path / "raw"
    _seed(src, {"a.gpkg": b"v1", "b.gpkg": b"v1"})
    dest = snapshot.create_snapshot(src, tmp_path / "snap", NAMES)
    (src / "a.gpkg").write_bytes(b"v2-changed-after-snapshot")
    assert (dest / "a.gpkg").read_bytes() == b"v1"


def test_snapshot_missing_input_raises_before_copying_anything(tmp_path):
    src = tmp_path / "raw"
    _seed(src, {"a.gpkg": b"AAAA"})  # b.gpkg absent
    dest = tmp_path / "snap"
    with pytest.raises(FileNotFoundError, match="missing inputs"):
        snapshot.create_snapshot(src, dest, NAMES)
    # All-or-nothing: the present file was not partially copied.
    assert not (dest / "a.gpkg").exists()


def test_snapshot_starts_clean_over_a_stale_dir(tmp_path):
    src = tmp_path / "raw"
    _seed(src, {"a.gpkg": b"AAAA", "b.gpkg": b"BBBB"})
    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "leftover.gpkg").write_bytes(b"stale")
    snapshot.create_snapshot(src, dest, NAMES)
    # A leftover from a crashed earlier run with this id does not survive.
    assert not (dest / "leftover.gpkg").exists()
    assert (dest / "a.gpkg").read_bytes() == b"AAAA"


def test_clear_snapshot_removes_the_directory(tmp_path):
    src = tmp_path / "raw"
    _seed(src, {"a.gpkg": b"AAAA", "b.gpkg": b"BBBB"})
    dest = snapshot.create_snapshot(src, tmp_path / "snap", NAMES)
    assert dest.exists()
    snapshot.clear_snapshot(dest)
    assert not dest.exists()


def test_clear_snapshot_is_safe_when_absent(tmp_path):
    # Idempotent cleanup: clearing a snapshot that is already gone is a no-op.
    snapshot.clear_snapshot(tmp_path / "never_made")


def test_snapshot_rejects_a_symlink_escaping_the_raw_dir(tmp_path):
    # A name in the raw dir that symlinks to a file outside it must be refused,
    # so external data is never copied into the snapshot and published.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.gpkg").write_bytes(b"external")
    src = tmp_path / "raw"
    src.mkdir()
    (src / "a.gpkg").write_bytes(b"AAAA")
    (src / "b.gpkg").symlink_to(outside / "secret.gpkg")
    with pytest.raises(ValueError, match="resolves outside"):
        snapshot.create_snapshot(src, tmp_path / "snap", NAMES)


def test_snapshot_accepts_a_symlink_to_a_sibling_in_the_raw_dir(tmp_path):
    # A symlink that stays inside the raw dir is a project input, not an escape:
    # the resolved target's parents include the raw dir, so it is snapshotted.
    src = tmp_path / "raw"
    src.mkdir()
    (src / "a.gpkg").write_bytes(b"AAAA")
    (src / "real_b.gpkg").write_bytes(b"BBBB")
    (src / "b.gpkg").symlink_to(src / "real_b.gpkg")
    dest = snapshot.create_snapshot(src, tmp_path / "snap", NAMES)
    assert (dest / "b.gpkg").read_bytes() == b"BBBB", "a same-directory symlink must be followed and copied"


def test_clear_snapshot_does_not_raise_on_unlink_error(tmp_path, monkeypatch):
    # After a successful promote the snapshot's cleanup must never fail the run;
    # an unlink error is swallowed so a published refresh is not marked failed.
    src = tmp_path / "raw"
    _seed(src, {"a.gpkg": b"AAAA", "b.gpkg": b"BBBB"})
    dest = snapshot.create_snapshot(src, tmp_path / "snap", NAMES)

    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(type(dest / "a.gpkg"), "unlink", boom)
    # Must not raise despite the unlink failure.
    snapshot.clear_snapshot(dest)


def test_snapshot_bytes_match_source(tmp_path):
    # The asset verifies the snapshot fingerprint against the launch tag, so the
    # snapshot must be byte-identical to the source it copied. Checked directly
    # on bytes so this runs without the optional orchestration extra (the
    # fingerprint helper lives in the Dagster-importing module).
    src = tmp_path / "raw"
    _seed(src, {"a.gpkg": b"AAAA", "b.gpkg": b"BBBB"})
    dest = snapshot.create_snapshot(src, tmp_path / "snap", NAMES)
    for name in NAMES:
        assert (dest / name).read_bytes() == (src / name).read_bytes()


def test_a_stale_input_folder_is_cleared_when_it_is_snapshotted_again(tmp_path):
    # A crashed run can leave snap/derived/ behind. The next run snapshots that
    # folder with its own create_snapshot call, whose clean start removes every
    # stale file, so a table dropped from data/derived/ cannot survive.
    derived = tmp_path / "derived"
    _seed(derived, {"kept.csv": b"k\n"})
    folder = tmp_path / "snap" / "derived"
    _seed(folder, {"kept.csv": b"old\n", "dropped.csv": b"stale\n"})
    snapshot.create_snapshot(derived, folder, ["kept.csv"])
    assert sorted(p.name for p in folder.iterdir()) == ["kept.csv"], "stale files must not survive"
    assert (folder / "kept.csv").read_bytes() == b"k\n"


def test_clear_snapshot_removes_the_input_folders_too(tmp_path):
    # A run's snapshot holds frame/ and derived/ beside the geopackages; clearing
    # it must remove them, or every run would leave a folder behind.
    src = tmp_path / "raw"
    _seed(src, {"a.gpkg": b"AAAA", "b.gpkg": b"BBBB"})
    frame = tmp_path / "frame"
    _seed(frame, {"index.csv": b"lga\n"})
    dest = snapshot.create_snapshot(src, tmp_path / "snap", NAMES)
    snapshot.create_snapshot(frame, dest / "frame", ["index.csv"])
    snapshot.clear_snapshot(dest)
    assert not dest.exists(), "the snapshot and its input folders should be gone"
