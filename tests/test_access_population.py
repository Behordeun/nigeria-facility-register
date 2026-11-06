"""Tests for nfr.enrichment.access_population and the committed derived table.

The zonal statistics run on small synthetic rasters whose expected values can be
worked out by hand: two square LGAs side by side, a fine population grid and a
coarse travel-time grid. The fetcher is tested without the network.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

rasterio = pytest.importorskip("rasterio")
gpd = pytest.importorskip("geopandas")
from rasterio.transform import from_origin  # noqa: E402
from shapely.geometry import box  # noqa: E402

from nfr.enrichment import access_population as ap  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DERIVED = ROOT / "data" / "derived" / "access_population_lga.csv"


def _write(path: Path, values: np.ndarray, cell: float, nodata: float | None = None) -> Path:
    with rasterio.open(
        path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1], count=1,
        dtype=values.dtype, crs="EPSG:4326", transform=from_origin(0.0, 2.0, cell, cell), nodata=nodata,
    ) as dst:
        dst.write(values, 1)
    return path


@pytest.fixture
def lgas() -> gpd.GeoDataFrame:
    # Two 1 x 2 degree LGAs: A on the left (x 0-1), B on the right (x 1-2).
    return gpd.GeoDataFrame(
        {"shapeID": ["A", "B"], "shapeName": ["Left", "Right"]},
        geometry=[box(0, 0, 1, 2), box(1, 0, 2, 2)], crs="EPSG:4326",
    )


@pytest.fixture
def population(tmp_path) -> Path:
    # 0.5-degree cells (4 x 4): left half 1 person per cell, right half 3 per cell,
    # except one right cell is nodata.
    values = np.array([
        [1, 1, 3, 3],
        [1, 1, 3, -99],
        [1, 1, 3, 3],
        [1, 1, 3, 3],
    ], dtype="float32")
    return _write(tmp_path / "pop.tif", values, 0.5, nodata=-99)


def test_population_totals_sum_each_lga_and_skip_nodata(population, lgas):
    totals = ap.population_totals(population, lgas)
    assert totals.tolist() == [8.0, 21.0], "left has 8 cells of 1; right has 7 valid cells of 3"


def test_population_totals_do_not_depend_on_strip_size(population, lgas):
    assert ap.population_totals(population, lgas, rows=1).tolist() == ap.population_totals(population, lgas, rows=4).tolist()


def test_population_on_a_coarser_grid_conserves_people(population):
    grid = ap.Grid(2, 2, from_origin(0.0, 2.0, 1.0, 1.0), rasterio.crs.CRS.from_epsg(4326))
    out = ap.population_on_grid(population, grid, rows=1)
    assert out.sum() == pytest.approx(29.0), "aggregation must neither create nor lose people"
    assert out[:, 0].tolist() == pytest.approx([4.0, 4.0]), "each 1-degree left cell holds four 1-person cells"
    assert out[0, 1] == pytest.approx(9.0), "the top-right cell has one nodata source cell"


def test_access_is_population_weighted_and_reports_share_over_threshold(population, lgas, tmp_path):
    # 1-degree travel grid (2 x 2): left column 30 and 90 minutes; right column
    # 120 (top) and nodata (bottom).
    minutes = np.array([[30, 120], [90, -1]], dtype="float32")
    travel = _write(tmp_path / "travel.tif", minutes, 1.0, nodata=-1)
    result = ap.access_by_lga(travel, population, lgas, threshold=60)
    left, right = result.iloc[0], result.iloc[1]
    assert left.mean_minutes == pytest.approx(60.0), "4 people at 30 and 4 at 90 average to 60"
    assert left.over_threshold_share == pytest.approx(0.5)
    assert right.mean_minutes == pytest.approx(120.0), "only the valid top-right cell counts"
    assert right.population_with_access_data == pytest.approx(9.0)


def test_access_is_null_when_an_lga_has_no_valid_travel_time(population, lgas, tmp_path):
    minutes = np.array([[30, -1], [90, -1]], dtype="float32")
    travel = _write(tmp_path / "travel.tif", minutes, 1.0, nodata=-1)
    result = ap.access_by_lga(travel, population, lgas, threshold=60)
    assert np.isnan(result.iloc[1].mean_minutes), "no valid cells must give null, never 0 minutes"
    assert np.isnan(result.iloc[1].over_threshold_share)


def test_check_within_bbox_rejects_lgas_outside_the_cut_out(lgas):
    ap.check_within_bbox(lgas, (-1, -1, 3, 3))
    with pytest.raises(ap.SourceError, match="outside the MAP box"):
        ap.check_within_bbox(lgas, (0.5, 0, 3, 3))


def test_map_coverage_url_cuts_to_the_bbox():
    url = ap.map_coverage_url("Cov_Id", (2.5, 4.0, 14.9, 14.0))
    assert "request=GetCoverage" in url and "coverageId=Cov_Id" in url
    assert "Lat%284.0000%2C14.0000%29" in url and "Long%282.5000%2C14.9000%29" in url


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_skips_a_file_that_already_matches_its_pin(tmp_path, monkeypatch):
    dest = tmp_path / "f.tif"
    dest.write_bytes(b"cached")
    pin = hashlib.sha256(b"cached").hexdigest()

    def no_network(*_args, **_kwargs):
        raise AssertionError("a pinned, present file must not be downloaded again")

    monkeypatch.setattr(ap.urllib.request, "urlopen", no_network)
    assert ap.fetch("https://example.invalid/f.tif", dest, pin) == pin


def test_fetch_rejects_a_download_that_does_not_match_the_pin(tmp_path, monkeypatch):
    dest = tmp_path / "f.tif"
    dest.write_bytes(b"old")
    monkeypatch.setattr(ap.urllib.request, "urlopen", lambda *_a, **_k: _Response(b"changed upstream"))
    with pytest.raises(ap.SourceError, match="does not match the pinned"):
        ap.fetch("https://example.invalid/f.tif", dest, hashlib.sha256(b"expected").hexdigest())
    assert dest.read_bytes() == b"old", "a rejected download must not replace the existing file"
    assert not list(tmp_path.glob(".f.tif.*.part")), "the temp file must be cleaned up"


def test_fetch_writes_a_new_file_and_returns_its_hash(tmp_path, monkeypatch):
    dest = tmp_path / "sub" / "f.tif"
    monkeypatch.setattr(ap.urllib.request, "urlopen", lambda *_a, **_k: _Response(b"fresh"))
    assert ap.fetch("https://example.invalid/f.tif", dest) == hashlib.sha256(b"fresh").hexdigest()
    assert dest.read_bytes() == b"fresh"


def _table(**overrides) -> pd.DataFrame:
    base = pd.DataFrame({
        "shapeID": [f"S{i}" for i in range(ap.EXPECTED_LGAS)],
        "shapeName": ["x"] * ap.EXPECTED_LGAS,
        "population_year": 2025,
        "population": 1000,
        "motorised_minutes": 10.0,
        "walking_minutes": 60.0,
        "walking_over_60min_share": 0.3,
        "access_population_share": 1.0,
    })
    for col, value in overrides.items():
        base.loc[0, col] = value
    return base


def test_check_accepts_a_well_formed_table():
    ap.check(_table())


@pytest.mark.parametrize("col,value,match", [
    ("population", 0, "no population"),
    ("walking_minutes", -1.0, "negative minutes"),
    ("walking_over_60min_share", 1.5, "outside 0-1"),
])
def test_check_rejects_a_broken_table(col, value, match):
    with pytest.raises(ap.SourceError, match=match):
        ap.check(_table(**{col: value}))


def test_load_lgas_requires_the_full_frame(tmp_path, lgas):
    boundaries = tmp_path / "b.geojson"
    lgas.to_file(boundaries, driver="GeoJSON")
    frame = tmp_path / "frame.csv"
    pd.DataFrame({"shapeID": ["A", "B"], "shapeName": ["Left", "Right"]}).to_csv(frame, index=False)
    with pytest.raises(ap.SourceError, match="774-LGA frame"):
        ap.load_lgas(boundaries, frame)


@pytest.mark.skipif(not DERIVED.exists(), reason="derived table not built")
def test_committed_table_covers_the_frame_with_valid_values():
    table = pd.read_csv(DERIVED)
    frame = pd.read_csv(ROOT / "data" / "frame" / "nigeria_master_lga_index.csv")
    assert set(table.shapeID) == set(frame.shapeID), "every frame LGA, and only those"
    ap.check(table)
