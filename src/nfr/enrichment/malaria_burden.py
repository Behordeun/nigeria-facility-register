"""
Per-LGA malaria burden from the Malaria Atlas Project.

Two public MAP rasters for one year are summarised onto the 774-LGA frame, both
for Plasmodium falciparum, which causes almost all malaria in Nigeria:

- Parasite rate (PfPR2-10): the modelled share of children aged 2-10 carrying
  the parasite, 0-1.
- Clinical incidence rate: modelled new clinical cases per person per year.

Both are MAP's 202608 release (annual surfaces 2000-2025, about 5 km, CC BY),
fetched as Nigeria cut-outs over WCS. Each LGA's figure is a population-weighted
mean (WorldPop R2025A for the same year), so it describes where people live, not
empty land. The weighting runs on the 100 m population grid: each 100 m cell
takes the value of the 5 km malaria cell it lies in, and is assigned to the LGA
its centre falls in. Working at 100 m matters here. Several dense urban LGAs
(Lagos Island, Shomolu, Tarauni and others) are smaller than one 5 km cell, so
assigning whole 5 km cells to LGAs, as the 1 km travel-time summary does, would
leave them with no cell at all.

Missing is not zero: an LGA with no population on valid malaria cells gets null
figures, never 0. The surfaces are model estimates with uncertainty, not case
counts; they say where malaria is relatively heavier, not how many cases a
facility sees.
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.warp import Resampling, reproject

from nfr.enrichment import access_population as ap

MAP_MALARIA_WCS_URL = "https://data.malariaatlas.org/geoserver/Malaria/wcs"
MAP_MALARIA_COVERAGES = {
    "pfpr": "Malaria__202608_Global_Pf_Parasite_Rate",
    "incidence": "Malaria__202608_Global_Pf_Incidence_Rate",
}
# The latest year the 202608 release publishes.
MALARIA_YEAR = 2025


def malaria_coverage_url(coverage_id: str, year: int, bbox: tuple[float, float, float, float] = ap.NIGERIA_BBOX) -> str:
    """WCS 2.0.1 GetCoverage URL for one MAP malaria coverage and year, cut to bbox.

    The box is the access module's fixed Nigeria box, so the request, and with it
    the downloaded bytes and their pinned hash, are the same on every run.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    query = [
        ("service", "WCS"),
        ("version", "2.0.1"),
        ("request", "GetCoverage"),
        ("coverageId", coverage_id),
        ("format", "image/tiff"),
        ("subset", f"Lat({lat_min:.4f},{lat_max:.4f})"),
        ("subset", f"Long({lon_min:.4f},{lon_max:.4f})"),
        ("subset", f'time("{year}-01-01T00:00:00.000Z")'),
    ]
    return f"{MAP_MALARIA_WCS_URL}?{urllib.parse.urlencode(query)}"


def _surface_on(strip: ap.Grid, surface) -> np.ndarray:
    """The surface's values on a population strip's grid, by the cell each lies in; NaN where invalid."""
    values = surface.read(1).astype("float64")
    values = np.where(ap._valid(values, surface.nodata) & (values >= 0), values, np.nan)
    out = np.full((strip.height, strip.width), np.nan)
    reproject(
        source=values, destination=out,
        src_transform=surface.transform, src_crs=surface.crs,
        dst_transform=strip.transform, dst_crs=strip.crs,
        resampling=Resampling.nearest, src_nodata=np.nan, dst_nodata=np.nan,
    )
    return out


def population_weighted_by_lga(
    surface_paths: dict[str, Path], pop_path: Path, lgas: gpd.GeoDataFrame, rows: int = ap.STRIP_ROWS
) -> pd.DataFrame:
    """Population-weighted mean of each surface per LGA, weighted on the 100 m population grid.

    Returns one column per surface plus population and population_with_data (the
    people on cells where every surface is valid), in lgas order. An LGA with no
    people on valid cells gets null means.
    """
    n = len(lgas) + 1
    population = np.zeros(n)
    covered = np.zeros(n)
    weighted = {key: np.zeros(n) for key in surface_paths}
    surfaces = {key: rasterio.open(path) for key, path in surface_paths.items()}
    try:
        with rasterio.open(pop_path) as src:
            for window, transform in ap._strips(src, rows):
                people = ap._people(src.read(1, window=window).astype("float64"), src.nodata)
                strip = ap.Grid(people.shape[0], people.shape[1], transform, src.crs)
                labels = ap._labels(lgas, strip).ravel()
                values = {key: _surface_on(strip, s).ravel() for key, s in surfaces.items()}
                ok = np.logical_and.reduce([np.isfinite(v) for v in values.values()])
                flat = people.ravel()
                population += np.bincount(labels, weights=flat, minlength=n)
                weight = np.where(ok, flat, 0.0)
                covered += np.bincount(labels, weights=weight, minlength=n)
                for key, v in values.items():
                    weighted[key] += np.bincount(labels, weights=weight * np.where(ok, v, 0.0), minlength=n)
    finally:
        for s in surfaces.values():
            s.close()
    out = pd.DataFrame({"population": population[1:], "population_with_data": covered[1:]})
    with np.errstate(invalid="ignore", divide="ignore"):
        for key in surface_paths:
            out[key] = np.where(covered[1:] > 0, weighted[key][1:] / covered[1:], np.nan)
    return out


def derive(pfpr_path: Path, incidence_path: Path, pop_path: Path, lgas: gpd.GeoDataFrame, year: int) -> pd.DataFrame:
    """The per-LGA malaria table, one row per frame LGA, in lgas order."""
    by_lga = population_weighted_by_lga({"pfpr": pfpr_path, "incidence": incidence_path}, pop_path, lgas)
    with np.errstate(invalid="ignore", divide="ignore"):
        coverage = np.where(by_lga["population"] > 0, by_lga["population_with_data"] / by_lga["population"], np.nan)
    return pd.DataFrame({
        "shapeID": lgas["shapeID"].to_numpy(),
        "shapeName": lgas["shapeName"].to_numpy(),
        "malaria_year": year,
        "pf_parasite_rate": by_lga["pfpr"].round(4).to_numpy(),
        "pf_incidence_per_1000": (by_lga["incidence"] * 1000).round(1).to_numpy(),
        "malaria_population_share": np.clip(coverage, 0, 1).round(4),
    })


def check(table: pd.DataFrame) -> None:
    """Refuse a table that breaks the guarantees the loader and dbt tests rely on."""
    problems = []
    if len(table) != ap.EXPECTED_LGAS or table["shapeID"].nunique() != ap.EXPECTED_LGAS:
        problems.append(f"expected {ap.EXPECTED_LGAS} LGAs once each, got {len(table)} rows")
    if table["malaria_year"].nunique() != 1:
        problems.append("the table must describe one malaria year")
    rate = table["pf_parasite_rate"].dropna()
    if ((rate < 0) | (rate > 1)).any():
        problems.append("pf_parasite_rate must lie in 0-1")
    if (table["pf_incidence_per_1000"].dropna() < 0).any():
        problems.append("pf_incidence_per_1000 must not be negative")
    if table["pf_parasite_rate"].isna().ne(table["pf_incidence_per_1000"].isna()).any():
        problems.append("parasite rate and incidence must be missing for the same LGAs")
    if problems:
        raise ValueError("malaria table failed its checks: " + "; ".join(problems))
