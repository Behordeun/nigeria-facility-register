"""Unit tests for the refresh sensor's change-detection fingerprint.

The sensor rebuilds only when the raw geopackages actually change, so the
fingerprint must be stable for unchanged files and differ when a file appears,
grows, or is replaced with different content (even at the same size and mtime).
Skipped when dagster is not installed (the orchestration extra), because the
module imports dagster at the top.
"""
import pytest

pytest.importorskip("dagster")  # the module imports dagster at top level

from nfr.orchestration.definitions import fingerprint  # noqa: E402


def test_stable_for_unchanged_files(tmp_path):
    (tmp_path / "a.gpkg").write_bytes(b"x" * 100)
    names = ["a.gpkg"]
    first = fingerprint(tmp_path, names)
    assert fingerprint(tmp_path, names) == first


def test_changes_when_a_file_appears(tmp_path):
    names = ["a.gpkg", "b.gpkg"]
    (tmp_path / "a.gpkg").write_bytes(b"x" * 100)
    before = fingerprint(tmp_path, names)
    (tmp_path / "b.gpkg").write_bytes(b"y" * 100)
    assert fingerprint(tmp_path, names) != before


def test_changes_when_only_one_of_a_pair_changes(tmp_path):
    # The combined fingerprint covers both inputs, so updating only one file
    # (a half-applied set) changes it. The sensor therefore does not mistake a
    # one-file update for the already-built vintage; combined with the
    # all-present guard, a mixed pair is never silently accepted as unchanged.
    names = ["a.gpkg", "b.gpkg"]
    (tmp_path / "a.gpkg").write_bytes(b"x" * 100)
    (tmp_path / "b.gpkg").write_bytes(b"y" * 100)
    before = fingerprint(tmp_path, names)
    (tmp_path / "a.gpkg").write_bytes(b"x" * 100 + b"NEW")  # only a changes
    assert fingerprint(tmp_path, names) != before


def test_changes_when_a_file_grows(tmp_path):
    names = ["a.gpkg"]
    (tmp_path / "a.gpkg").write_bytes(b"x" * 100)
    before = fingerprint(tmp_path, names)
    (tmp_path / "a.gpkg").write_bytes(b"x" * 200)
    assert fingerprint(tmp_path, names) != before


def test_empty_dir_is_stable(tmp_path):
    first = fingerprint(tmp_path, ["none.gpkg"])
    assert fingerprint(tmp_path, ["none.gpkg"]) == first


def test_detects_same_size_same_mtime_content_change(tmp_path):
    # A replacement vintage of identical byte length with a preserved mtime must
    # still change the fingerprint; hashing content, not size+mtime, catches it.
    import os

    f = tmp_path / "a.gpkg"
    f.write_bytes(b"AAAA")
    st = f.stat()
    before = fingerprint(tmp_path, ["a.gpkg"])
    f.write_bytes(b"BBBB")
    os.utime(f, (st.st_atime, st.st_mtime))
    assert fingerprint(tmp_path, ["a.gpkg"]) != before


def test_rejects_a_symlink_escaping_the_watched_dir(tmp_path):
    # A name inside the watched dir that symlinks outside it must be refused,
    # so the sensor never hashes bytes beyond the directory it watches.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"x")
    watched = tmp_path / "raw"
    watched.mkdir()
    (watched / "a.gpkg").symlink_to(outside / "secret")
    with pytest.raises(ValueError, match="escapes the watched directory"):
        fingerprint(watched, ["a.gpkg"])
