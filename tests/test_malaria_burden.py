"""Tests for nfr.enrichment.malaria_burden and the committed derived table.

The weighting runs on small synthetic rasters whose expected values can be worked
out by hand: a fine population grid, a coarse malaria grid, and LGAs including
one smaller than a single malaria cell.
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

rasterio = pytest.importorskip("rasterio")
gpd = pytest.importorskip("geopandas")
from rasterio.transform import from_origin  # noqa: E402
from shapely.geometry import box  # noqa: E402

from nfr.enrichment import access_population as ap  # noqa: E402
from nfr.enrichment import malaria_burden as mb  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DERIVED = ROOT / "data" / "derived" / "malaria_lga.csv"


def _write(path: Path, values: np.ndarray, cell: float, nodata: float | None = None) -> Path:
    with rasterio.open(
        path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1], count=1,
        dtype=values.dtype, crs="EPSG:4326", transform=from_origin(0.0, 2.0, cell, cell), nodata=nodata,
    ) as dst:
        dst.write(values, 1)
    return path


@pytest.fixture
def population(tmp_path) -> Path:
    # 0.25-degree cells over a 2 x 2 degree square (8 x 8): one person per cell,
    # except the top-left quarter-degree cell, which holds 9.
    values = np.ones((8, 8), dtype="float32")
    values[0, 0] = 9
    return _write(tmp_path / "pop.tif", values, 0.25)


@pytest.fixture
def surfaces(tmp_path) -> dict[str, Path]:
    # 1-degree malaria grid (2 x 2): parasite rate 0.2 / 0.4 on the top row and
    # 0.3 / nodata on the bottom; incidence 0.25 per person-year everywhere valid.
    pfpr = np.array([[0.2, 0.4], [0.3, -9999]], dtype="float32")
    incidence = np.array([[0.25, 0.25], [0.25, -9999]], dtype="float32")
    return {
        "pfpr": _write(tmp_path / "pfpr.tif", pfpr, 1.0, nodata=-9999),
        "incidence": _write(tmp_path / "incidence.tif", incidence, 1.0, nodata=-9999),
    }


def test_weights_by_population_within_each_malaria_cell(population, surfaces):
    # One LGA covering the left degree column: the top cell (0.2) holds 9 + 15 = 24
    # people, the bottom cell (0.3) holds 16.
    lgas = gpd.GeoDataFrame({"shapeID": ["L"], "shapeName": ["Left"]}, geometry=[box(0, 0, 1, 2)], crs="EPSG:4326")
    out = mb.population_weighted_by_lga(surfaces, population, lgas, rows=3)
    expected = (24 * 0.2 + 16 * 0.3) / 40
    assert out.loc[0, "pfpr"] == pytest.approx(expected), "people, not area, decide the mean"
    assert out.loc[0, "population"] == pytest.approx(40.0)


def test_an_lga_smaller_than_one_malaria_cell_still_gets_its_value(population, surfaces):
    # A quarter-degree LGA inside the top-right malaria cell (0.4): no 1-degree
    # cell centre falls inside it, but its people do.
    lgas = gpd.GeoDataFrame(
        {"shapeID": ["S"], "shapeName": ["Small"]}, geometry=[box(1.25, 1.25, 1.5, 1.5)], crs="EPSG:4326")
    out = mb.population_weighted_by_lga(surfaces, population, lgas)
    assert out.loc[0, "pfpr"] == pytest.approx(0.4), "a small urban LGA must not come out empty"


def test_people_on_invalid_cells_are_left_out_and_an_lga_with_none_valid_is_null(population, surfaces):
    lgas = gpd.GeoDataFrame(
        {"shapeID": ["R", "N"], "shapeName": ["Right", "Nodata"]},
        geometry=[box(1, 0, 2, 2), box(1, 0, 2, 1)], crs="EPSG:4326")
    out = mb.population_weighted_by_lga(surfaces, population, lgas)
    right, nodata_only = out.iloc[0], out.iloc[1]
    assert right["pfpr"] == pytest.approx(0.4), "only the valid top-right cell counts"
    assert right["population_with_data"] == pytest.approx(16.0)
    assert np.isnan(nodata_only["pfpr"]), "no valid cells must give null, never 0"


def test_derive_reports_incidence_per_1000_and_the_covered_share(population, surfaces):
    lgas = gpd.GeoDataFrame({"shapeID": ["R"], "shapeName": ["Right"]}, geometry=[box(1, 0, 2, 2)], crs="EPSG:4326")
    table = mb.derive(surfaces["pfpr"], surfaces["incidence"], population, lgas, 2025)
    row = table.iloc[0]
    assert row["pf_incidence_per_1000"] == pytest.approx(250.0)
    assert row["malaria_population_share"] == pytest.approx(0.5), "half the people sit on the nodata cell"
    assert row["malaria_year"] == 2025


def test_the_coverage_url_asks_the_malaria_workspace_for_one_year_and_the_fixed_box():
    url = mb.malaria_coverage_url("Malaria__202608_Global_Pf_Parasite_Rate", 2025)
    assert url.startswith(mb.MAP_MALARIA_WCS_URL + "?")
    subsets = [v for k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query) if k == "subset"]
    assert subsets == ["Lat(4.0000,14.0000)", "Long(2.5000,14.9000)", 'time("2025-01-01T00:00:00.000Z")']


def _table(**overrides) -> pd.DataFrame:
    table = pd.DataFrame({
        "shapeID": [f"S{i}" for i in range(ap.EXPECTED_LGAS)],
        "shapeName": [f"L{i}" for i in range(ap.EXPECTED_LGAS)],
        "malaria_year": 2025,
        "pf_parasite_rate": 0.25,
        "pf_incidence_per_1000": 270.0,
        "malaria_population_share": 1.0,
    })
    for column, value in overrides.items():
        table.loc[0, column] = value
    return table


def test_check_accepts_a_well_formed_table():
    mb.check(_table())


def test_check_rejects_a_table_for_another_year():
    table = _table()
    table["malaria_year"] = mb.MALARIA_YEAR - 1
    with pytest.raises(ValueError, match=f"must be {mb.MALARIA_YEAR}"):
        mb.check(table)


def test_a_surface_that_fails_to_open_does_not_leave_the_others_open(population, surfaces, tmp_path, monkeypatch):
    opened = []
    real_open = rasterio.open

    def tracking_open(path, *args, **kwargs):
        dataset = real_open(path, *args, **kwargs)
        opened.append(dataset)
        return dataset

    monkeypatch.setattr(mb.rasterio, "open", tracking_open)
    lgas = gpd.GeoDataFrame({"shapeID": ["L"], "shapeName": ["Left"]}, geometry=[box(0, 0, 1, 2)], crs="EPSG:4326")
    paths = {"pfpr": surfaces["pfpr"], "incidence": tmp_path / "missing.tif"}
    with pytest.raises(rasterio.errors.RasterioIOError):
        mb.population_weighted_by_lga(paths, population, lgas)
    assert opened and all(d.closed for d in opened), "a surface opened before the failure must be closed"


@pytest.mark.parametrize("column, value, match", [
    ("pf_parasite_rate", 1.5, "0-1"),
    ("pf_incidence_per_1000", -1.0, "negative"),
    ("pf_parasite_rate", np.nan, "same LGAs"),
    ("malaria_year", 2024, "one malaria year"),
])
def test_check_rejects_a_broken_table(column, value, match):
    with pytest.raises(ValueError, match=match):
        mb.check(_table(**{column: value}))


@pytest.mark.skipif(not DERIVED.exists(), reason="derived table not built")
def test_committed_table_covers_the_frame_and_matches_its_pinned_year():
    table = pd.read_csv(DERIVED)
    frame = pd.read_csv(ROOT / "data" / "frame" / "nigeria_master_lga_index.csv")
    assert set(table.shapeID) == set(frame.shapeID), "every frame LGA, and only those"
    mb.check(table)
    entry = yaml.safe_load((ROOT / "data" / "sources.yml").read_text())["sources"]["malaria_lga"]
    assert table["malaria_year"].iloc[0] == entry["malaria_year"] == mb.MALARIA_YEAR
    assert ap.sha256(DERIVED) == entry["sha256"], "record the committed table's hash in data/sources.yml"
