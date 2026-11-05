"""Tests for the vendored LGA boundary artifact.

The choropleth joins boundary polygons to the marts on shapeID, so the vendored
file must have exactly the 774 LGAs, join 1:1 to the frame, carry valid
geometry, and stay small enough to belong in the repo. geopandas is a hard dep,
so these run in the base suite.
"""
from __future__ import annotations

import csv
from pathlib import Path

import geopandas as gpd

ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ROOT / "data" / "frame" / "nigeria_774_lga_boundaries.simplified.geojson"
FRAME_INDEX = ROOT / "data" / "frame" / "nigeria_master_lga_index.csv"


def _frame_shape_ids() -> set[str]:
    with open(FRAME_INDEX) as fh:
        return {row["shapeID"] for row in csv.DictReader(fh) if row.get("shapeID")}


def test_boundary_artifact_exists():
    assert BOUNDARIES.is_file(), "vendored LGA boundary geojson is missing"


def test_boundaries_have_774_features_with_the_join_key():
    gdf = gpd.read_file(BOUNDARIES)
    assert len(gdf) == 774
    assert "shapeID" in gdf.columns
    # shapeID is the join key, so it must be unique and non-null.
    assert gdf["shapeID"].notna().all()
    assert gdf["shapeID"].is_unique


def test_boundaries_join_the_frame_one_to_one():
    gdf = gpd.read_file(BOUNDARIES)
    frame_ids = _frame_shape_ids()
    boundary_ids = set(gdf["shapeID"])
    assert boundary_ids == frame_ids, (
        f"{len(boundary_ids - frame_ids)} boundary ids not in frame, "
        f"{len(frame_ids - boundary_ids)} frame ids not in boundaries"
    )


def test_boundary_geometry_is_valid():
    gdf = gpd.read_file(BOUNDARIES)
    assert gdf.geometry.is_valid.all(), "some boundary geometries are invalid"
    assert not gdf.geometry.is_empty.any(), "some boundary geometries are empty"


def test_boundary_file_is_simplified_not_the_raw_download():
    # The committed artifact must be the simplified set (~1.5 MB), not the ~9 MB
    # raw geojson. Guard against a future re-commit of the full-precision file.
    size_mb = BOUNDARIES.stat().st_size / (1024 * 1024)
    assert size_mb < 3.0, f"boundary file is {size_mb:.1f} MB; expected the simplified ~1.5 MB set"
