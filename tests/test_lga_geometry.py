"""Tests for the vendored LGA boundary artifact.

The choropleth joins boundary polygons to the marts on shapeID, so the vendored
file must cover exactly the 774 LGAs (as distinct shapeIDs; MultiPolygon LGAs are
pre-split into one single-Polygon feature per part, so the feature count is a
little above 774), join to the frame on shapeID, carry valid single-Polygon
geometry, and be genuinely simplified (not the raw full-precision download).
geopandas is imported via importorskip so a missing optional geo stack skips
these cleanly rather than erroring collection.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

gpd = pytest.importorskip("geopandas")

ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ROOT / "data" / "frame" / "nigeria_774_lga_boundaries.simplified.geojson"
FRAME_INDEX = ROOT / "data" / "frame" / "nigeria_master_lga_index.csv"


def _frame_shape_ids() -> set[str]:
    # `is not None`, not truthiness: a blank shapeID must count as a present (but
    # corrupt) key so the join check below can catch it, not be silently dropped.
    with open(FRAME_INDEX) as fh:
        return {row["shapeID"] for row in csv.DictReader(fh) if row.get("shapeID") is not None}


def _vertex_count(geom) -> int:
    # Total coordinate count across a (Multi)Polygon's rings.
    polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    n = 0
    for poly in polys:
        n += len(poly.exterior.coords)
        n += sum(len(r.coords) for r in poly.interiors)
    return n


def test_boundary_artifact_exists():
    assert BOUNDARIES.is_file(), "vendored LGA boundary geojson is missing"


def test_boundaries_cover_774_lgas_with_the_join_key():
    # MultiPolygon LGAs are pre-split into one feature per part, so the feature
    # count exceeds 774 while the distinct shapeID count is exactly 774. Parts of
    # one LGA repeat its shapeID, so shapeID is no longer unique per row; the LGA
    # identity lives in the distinct shapeID set.
    gdf = gpd.read_file(BOUNDARIES)
    assert "shapeID" in gdf.columns
    assert gdf["shapeID"].notna().all()
    assert gdf["shapeID"].nunique() == 774
    assert len(gdf) >= 774


def test_boundaries_join_the_frame_one_to_one():
    gdf = gpd.read_file(BOUNDARIES)
    frame_ids = _frame_shape_ids()
    boundary_ids = set(gdf["shapeID"])
    assert boundary_ids == frame_ids, (
        f"{len(boundary_ids - frame_ids)} boundary ids not in frame, "
        f"{len(frame_ids - boundary_ids)} frame ids not in boundaries"
    )


def test_boundary_geometry_is_single_polygon_valid_and_nonempty():
    # After the explode every feature is a single Polygon, never a MultiPolygon:
    # a deck.gl polygon layer rejects a MultiPolygon, which is the reason for the
    # split.
    gdf = gpd.read_file(BOUNDARIES)
    assert gdf.geometry.geom_type.eq("Polygon").all(), "expected single Polygons only"
    assert gdf.geometry.is_valid.all(), "invalid geometry"
    assert not gdf.geometry.is_empty.any(), "empty geometry"


def test_boundaries_are_genuinely_simplified_not_the_raw_download():
    # Size alone is a weak proxy (a wrong small polygon set would pass), so check
    # the geometry is actually coarser than full precision: a simplified national
    # LGA set averages well under ~150 vertices per feature, whereas the raw
    # geoBoundaries ADM2 features run into the hundreds-to-thousands. This also
    # fails if someone re-commits the raw full-precision file.
    gdf = gpd.read_file(BOUNDARIES)
    avg_vertices = sum(_vertex_count(g) for g in gdf.geometry) / len(gdf)
    assert avg_vertices < 150, (
        f"average {avg_vertices:.0f} vertices/feature looks unsimplified; "
        "expected the topology-simplified set, not the raw download"
    )
    # And still a sane lower bound: not degenerate 3-point boxes.
    assert avg_vertices > 10, f"average {avg_vertices:.0f} vertices/feature is implausibly coarse"
