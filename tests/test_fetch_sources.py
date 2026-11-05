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


def test_validate_rejects_a_non_gpkg_ogr_format_named_gpkg(tmp_path):
    # A different OGR-readable format (GeoJSON) under a .gpkg name must be
    # rejected by the driver check, not accepted because it yields layers.
    import geopandas as gpd
    from shapely.geometry import Point

    gdf = gpd.GeoDataFrame({"geometry": [Point(3.4, 6.5)]}, crs="EPSG:4326")
    p = tmp_path / "nga_health_facilities_v2_0.gpkg"
    gdf.to_file(p, driver="GeoJSON")
    # Pad over the size floor so it reaches the driver check.
    with p.open("ab") as fh:
        fh.write(b" " * fetch_sources.MIN_GPKG_BYTES)
    ok, reason = fetch_sources.validate_geopackage(p)
    assert not ok
    assert "not a GeoPackage" in reason


def test_validate_rejects_a_geopackage_with_no_features(tmp_path):
    # A real GPKG whose layer has zero features must be rejected: a correctly
    # named but empty source would otherwise install and the loader ingest zero.
    import geopandas as gpd

    empty = gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")
    p = tmp_path / "nga_health_facilities_v2_0.gpkg"
    empty.to_file(p, driver="GPKG")
    ok, reason = fetch_sources.validate_geopackage(p)
    assert not ok
    assert "no features" in reason


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


def test_place_publishes_only_via_atomic_replace(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    monkeypatch.setattr(fetch_sources, "RAW", raw)
    download = tmp_path / EXPECTED_NAME
    download.write_bytes(SAMPLE_V2.read_bytes())

    # Spy on os.replace: assert the destination does NOT exist until replace is
    # called, so a future change to direct-write-at-dest (no atomic rename) is
    # caught rather than passing because the end state looks right.
    dest = raw / EXPECTED_NAME
    real_replace = fetch_sources.os.replace
    seen = {}

    def spy_replace(src, dst, *a, **k):
        seen["dest_before"] = Path(dst).exists()
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(fetch_sources.os, "replace", spy_replace)
    result = fetch_sources.place(download)

    assert result == dest
    assert dest.is_file()
    assert seen["dest_before"] is False, "destination existed before os.replace: not atomic"
    ok, _ = fetch_sources.validate_geopackage(dest)
    assert ok
    assert not list(raw.glob(".*.part.gpkg"))
    # The source download is left in place (this places a copy, does not move).
    assert download.is_file()


def test_placed_file_is_group_readable_not_world_readable(tmp_path, monkeypatch):
    # The placed file must be readable by the owner and group (so a sensor/loader
    # account in the same group can read it) but not world-readable, since the
    # raw geopackages should not be exposed to every account. mkstemp alone would
    # leave it owner-only; a 0o644 would expose it to the world.
    import os
    import stat

    raw = tmp_path / "raw"
    monkeypatch.setattr(fetch_sources, "RAW", raw)
    download = tmp_path / EXPECTED_NAME
    download.write_bytes(SAMPLE_V2.read_bytes())
    monkeypatch.setattr(fetch_sources, "_umask", lambda: 0)  # isolate from the runner's umask
    dest = fetch_sources.place(download)
    mode = stat.S_IMODE(os.stat(dest).st_mode)
    assert mode & stat.S_IRGRP, "placed file must be group-readable"
    assert not (mode & stat.S_IROTH), "placed file must not be world-readable"


def test_place_all_lands_both_or_neither(tmp_path, monkeypatch):
    # A set place with one bad file must promote nothing, so the refresh never
    # sees one new file beside one old one.
    raw = tmp_path / "raw"
    monkeypatch.setattr(fetch_sources, "RAW", raw)
    good = tmp_path / "nga_health_facilities_v2_0.gpkg"
    good.write_bytes(SAMPLE_V2.read_bytes())
    bad = tmp_path / "grid3_nga_health_facilities_v3_0.gpkg"
    bad.write_bytes(b"<html>challenge</html>")

    with pytest.raises(SystemExit):
        fetch_sources.place_all([good, bad])

    # Neither landed, and no temp files are left behind.
    assert not (raw / good.name).exists()
    assert not (raw / bad.name).exists()
    assert raw.exists() and not list(raw.glob(".*.part.gpkg"))
