"""Tests for the raw-input placement and validation in scripts/fetch_sources.py.

The script lives under scripts/, not the package, so it is imported by path.
geopandas/pyogrio are hard deps of the project, so these run in the base suite.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fetch_sources",
    Path(__file__).resolve().parents[1] / "scripts" / "fetch_sources.py",
)
fetch_sources = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fetch_sources)

SAMPLE_V2 = Path(__file__).resolve().parents[1] / "data" / "sample" / "nga_health_facilities_v2_0.gpkg"
EXPECTED_NAME = "nga_health_facilities_v2_0.gpkg"


def test_validate_accepts_a_real_geopackage():
    ok, reason = fetch_sources.validate_geopackage(SAMPLE_V2)
    assert ok, reason


def test_validate_rejects_a_missing_file(tmp_path):
    ok, reason = fetch_sources.validate_geopackage(tmp_path / "nope.gpkg")
    assert not ok
    assert "not a file" in reason


def test_validate_rejects_a_truncated_file(tmp_path):
    # A small non-geopackage (e.g. a WAF challenge HTML page) must be rejected.
    p = tmp_path / "x.gpkg"
    p.write_bytes(b"<html>challenge</html>")
    ok, reason = fetch_sources.validate_geopackage(p)
    assert not ok
    assert "too small" in reason


def test_validate_rejects_a_large_non_geopackage(tmp_path):
    # Over the size floor but not a geopackage: must fail on the open, not pass.
    p = tmp_path / "big.gpkg"
    p.write_bytes(b"\x00" * (fetch_sources.MIN_GPKG_BYTES + 10))
    ok, reason = fetch_sources.validate_geopackage(p)
    assert not ok
    assert "not a readable geopackage" in reason


def test_place_rejects_an_unexpected_name(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_sources, "RAW", tmp_path / "raw")
    bad = tmp_path / "some_other_name.gpkg"
    bad.write_bytes(SAMPLE_V2.read_bytes())
    with pytest.raises(SystemExit, match="unexpected file name"):
        fetch_sources.place(bad)


def test_place_rejects_an_invalid_download(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_sources, "RAW", tmp_path / "raw")
    bad = tmp_path / EXPECTED_NAME
    bad.write_bytes(b"<html>challenge</html>")
    with pytest.raises(SystemExit, match="refusing to place"):
        fetch_sources.place(bad)
    # Nothing landed in data/raw/
    assert not (tmp_path / "raw" / EXPECTED_NAME).exists()


def test_place_moves_a_valid_download_atomically(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    monkeypatch.setattr(fetch_sources, "RAW", raw)
    download = tmp_path / EXPECTED_NAME
    download.write_bytes(SAMPLE_V2.read_bytes())
    dest = fetch_sources.place(download)
    assert dest == raw / EXPECTED_NAME
    assert dest.is_file()
    # The placed file is a valid geopackage and no .part temp remains.
    ok, _ = fetch_sources.validate_geopackage(dest)
    assert ok
    assert not list(raw.glob(".*.part"))
