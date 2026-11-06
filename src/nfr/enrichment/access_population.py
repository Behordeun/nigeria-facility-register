"""
Per-LGA population and access to healthcare, from WorldPop and the Malaria Atlas Project.

Two public rasters are summarised onto the 774-LGA frame:

- WorldPop Global2 R2025A constrained population counts for Nigeria (about 100 m,
  CC BY 4.0, DOI 10.5258/SOTON/WP00839): people per grid cell for a given year.
- Malaria Atlas Project travel time to healthcare (2020 release, Weiss et al.,
  Nature Medicine 2020; about 1 km; CC BY): minutes to the nearest health
  facility, once by motorised transport and once walking only.

The output has one row per LGA (keyed on the frame's geoBoundaries shapeID):

- population: total WorldPop population, summed at the native 100 m grid.
- motorised_minutes / walking_minutes: population-weighted mean travel time, so
  the figure describes where people live, not the LGA's empty land. Population is
  aggregated onto the 1 km travel-time grid (a sum, so no people are created or
  lost) and each 1 km cell is weighted by the people in it.
- walking_over_60min_share: share of the LGA's population (on cells with a valid
  walking time) more than 60 minutes' walk from the nearest facility.
- access_population_share: share of the LGA's population that sits on cells with
  a valid travel time; the travel figures describe only that population.

Missing is not zero: an LGA with no population on valid travel-time cells gets
null travel figures, never 0. Each cell belongs to exactly one LGA (the one its
centre falls in), so LGA totals partition the national total without double
counting.

The fetchers are idempotent and verify content: a file already on disk with the
expected SHA-256 is not downloaded again, and a download whose hash does not
match the pin is rejected before it replaces anything. That keeps the same code
usable from a one-off build and, later, from a scheduled refresh.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio import features
from rasterio.enums import Resampling
from rasterio.warp import reproject
from rasterio.windows import Window

WORLDPOP_URL = (
    "https://data.worldpop.org/GIS/Population/Global_2015_2030/R2025A/{year}/NGA/v1/100m/"
    "constrained/nga_pop_{year}_CN_100m_R2025A_v1.tif"
)
MAP_WCS_URL = "https://data.malariaatlas.org/geoserver/Accessibility/wcs"
MAP_COVERAGES = {
    "motorised": "Accessibility__202001_Global_Motorized_Travel_Time_to_Healthcare",
    "walking": "Accessibility__202001_Global_Walking_Only_Travel_Time_To_Healthcare",
}
WALKING_THRESHOLD_MINUTES = 60
# The MAP rasters are cut to this fixed lon/lat box (west, south, east, north),
# not to bounds computed from whichever boundary file is passed: a fixed box
# makes the request, and so the downloaded bytes and their pinned hash, the same
# on every run. It contains Nigeria with a margin; load_lgas-based callers check
# that every LGA falls inside it.
NIGERIA_BBOX = (2.5, 4.0, 14.9, 14.0)
DOWNLOAD_TIMEOUT_S = 120
CHUNK = 1 << 20
# Rows of the 100 m population raster processed at a time (about 12,000 cells
# wide), which keeps memory to tens of megabytes per strip.
STRIP_ROWS = 1024
EXPECTED_LGAS = 774


class SourceError(RuntimeError):
    """A source file could not be fetched, or did not match its pinned content."""


@dataclass(frozen=True)
class Grid:
    """A raster grid: shape, affine transform and CRS."""

    height: int
    width: int
    transform: rasterio.Affine
    crs: object


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def map_coverage_url(coverage_id: str, bbox: tuple[float, float, float, float]) -> str:
    """WCS 2.0.1 GetCoverage URL for one MAP coverage, cut to bbox (lon/lat degrees)."""
    lon_min, lat_min, lon_max, lat_max = bbox
    query = [
        ("service", "WCS"),
        ("version", "2.0.1"),
        ("request", "GetCoverage"),
        ("coverageId", coverage_id),
        ("format", "image/tiff"),
        ("subset", f"Lat({lat_min:.4f},{lat_max:.4f})"),
        ("subset", f"Long({lon_min:.4f},{lon_max:.4f})"),
    ]
    return f"{MAP_WCS_URL}?{urllib.parse.urlencode(query)}"


def fetch(url: str, dest: Path, expected_sha256: str | None = None) -> str:
    """Download url to dest atomically and return the file's SHA-256.

    If dest already exists and matches expected_sha256 (or no hash is pinned),
    nothing is downloaded. A download whose hash differs from the pin raises
    SourceError and leaves any existing dest untouched.
    """
    if dest.is_file():
        current = sha256(dest)
        if expected_sha256 is None or current == expected_sha256:
            return current
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=f".{dest.name}.", suffix=".part")
    tmp = Path(tmp_name)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "nigeria-facility-register"})
        # Wrap the descriptor before the network call, so it is closed even when
        # the request itself fails.
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:
            for chunk in iter(lambda: response.read(CHUNK), b""):
                out.write(chunk)
        got = sha256(tmp)
        if expected_sha256 is not None and got != expected_sha256:
            raise SourceError(
                f"{dest.name}: downloaded SHA-256 {got[:12]} does not match the pinned "
                f"{expected_sha256[:12]}; the source has changed, so review it and update the pin"
            )
        os.replace(tmp, dest)
        return got
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_lgas(boundaries: Path, frame_index: Path) -> gpd.GeoDataFrame:
    """One geometry per frame LGA (parts dissolved by shapeID), in EPSG:4326.

    Raises SourceError unless the boundaries cover exactly the frame's shapeIDs.
    """
    shapes = gpd.read_file(boundaries).to_crs("EPSG:4326")
    lgas = shapes[["shapeID", "geometry"]].dissolve(by="shapeID").reset_index()
    frame = pd.read_csv(frame_index, usecols=["shapeID", "shapeName"]).drop_duplicates("shapeID")
    missing = set(frame.shapeID) - set(lgas.shapeID)
    extra = set(lgas.shapeID) - set(frame.shapeID)
    if missing or extra or len(frame) != EXPECTED_LGAS:
        raise SourceError(
            f"boundaries do not match the 774-LGA frame: {len(missing)} missing, {len(extra)} extra"
        )
    return lgas.merge(frame, on="shapeID").sort_values("shapeID").reset_index(drop=True)


def check_within_bbox(lgas: gpd.GeoDataFrame, bbox: tuple[float, float, float, float] = NIGERIA_BBOX) -> None:
    """Raise SourceError unless every LGA lies inside the fixed MAP cut-out box."""
    lon_min, lat_min, lon_max, lat_max = lgas.total_bounds
    west, south, east, north = bbox
    if lon_min < west or lat_min < south or lon_max > east or lat_max > north:
        raise SourceError(f"LGA bounds {lgas.total_bounds.round(3).tolist()} fall outside the MAP box {bbox}")


def _grid(src) -> Grid:
    return Grid(src.height, src.width, src.transform, src.crs)


def _labels(lgas: gpd.GeoDataFrame, grid: Grid) -> np.ndarray:
    """Raster of LGA index + 1 per cell (0 = outside every LGA), by cell centre."""
    shapes = ((geom, i + 1) for i, geom in enumerate(lgas.geometry))
    return features.rasterize(
        shapes, out_shape=(grid.height, grid.width), transform=grid.transform,
        fill=0, dtype="int32", all_touched=False,
    )


def _valid(values: np.ndarray, nodata: float | None) -> np.ndarray:
    ok = np.isfinite(values)
    if nodata is not None:
        ok &= values != nodata
    return ok


def _strips(src, rows: int):
    """Yield (window, transform) row strips covering the whole raster."""
    for row in range(0, src.height, rows):
        window = Window(0, row, src.width, min(rows, src.height - row))
        yield window, src.window_transform(window)


def _people(values: np.ndarray, nodata: float | None) -> np.ndarray:
    return np.where(_valid(values, nodata) & (values > 0), values, 0.0)


def population_totals(pop_path: Path, lgas: gpd.GeoDataFrame, rows: int = STRIP_ROWS) -> np.ndarray:
    """Total population per LGA (in lgas order), summed on the native grid.

    Read in row strips so the 100 m national raster never sits in memory whole.
    """
    totals = np.zeros(len(lgas) + 1, dtype="float64")
    with rasterio.open(pop_path) as src:
        for window, transform in _strips(src, rows):
            values = _people(src.read(1, window=window).astype("float64"), src.nodata)
            strip = Grid(values.shape[0], values.shape[1], transform, src.crs)
            labels = _labels(lgas, strip)
            totals += np.bincount(labels.ravel(), weights=values.ravel(), minlength=len(lgas) + 1)
    return totals[1:]


def population_on_grid(pop_path: Path, grid: Grid, rows: int = STRIP_ROWS) -> np.ndarray:
    """Population aggregated onto another grid by summation (people are conserved).

    Strips are summed independently and added: GDAL's sum resampling counts only
    the source pixels present, so partial strips combine without double counting.
    """
    out = np.zeros((grid.height, grid.width), dtype="float64")
    with rasterio.open(pop_path) as src:
        for window, transform in _strips(src, rows):
            values = _people(src.read(1, window=window).astype("float64"), src.nodata)
            part = np.zeros_like(out)
            reproject(
                source=values, destination=part,
                src_transform=transform, src_crs=src.crs,
                dst_transform=grid.transform, dst_crs=grid.crs,
                resampling=Resampling.sum, src_nodata=None, dst_nodata=None,
            )
            out += part
    return out


def access_by_lga(
    travel_path: Path, pop_path: Path, lgas: gpd.GeoDataFrame, threshold: float | None = None
) -> pd.DataFrame:
    """Population-weighted travel time per LGA (and the share over threshold, if given).

    Returns columns mean_minutes, over_threshold_share (when threshold is set) and
    population_with_access_data, in lgas order. LGAs with no population on valid
    travel-time cells get null figures.
    """
    with rasterio.open(travel_path) as src:
        minutes = src.read(1, masked=False).astype("float64")
        ok = _valid(minutes, src.nodata) & (minutes >= 0)
        grid = _grid(src)
    labels = _labels(lgas, grid)
    pop = population_on_grid(pop_path, grid)
    weight = np.where(ok, pop, 0.0).ravel()
    flat_labels = labels.ravel()
    n = len(lgas) + 1
    pop_sum = np.bincount(flat_labels, weights=weight, minlength=n)[1:]
    weighted = np.bincount(flat_labels, weights=weight * np.where(ok, minutes, 0.0).ravel(), minlength=n)[1:]
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(pop_sum > 0, weighted / pop_sum, np.nan)
    result = pd.DataFrame({"mean_minutes": mean, "population_with_access_data": pop_sum})
    if threshold is not None:
        over = np.bincount(flat_labels, weights=weight * (np.where(ok, minutes, 0.0) > threshold).ravel(), minlength=n)[1:]
        with np.errstate(invalid="ignore", divide="ignore"):
            result["over_threshold_share"] = np.where(pop_sum > 0, over / pop_sum, np.nan)
    return result


def derive(
    pop_path: Path, motorised_path: Path, walking_path: Path, lgas: gpd.GeoDataFrame, year: int
) -> pd.DataFrame:
    """The per-LGA access and population table, one row per frame LGA."""
    population = population_totals(pop_path, lgas)
    motorised = access_by_lga(motorised_path, pop_path, lgas)
    walking = access_by_lga(walking_path, pop_path, lgas, threshold=WALKING_THRESHOLD_MINUTES)
    with np.errstate(invalid="ignore", divide="ignore"):
        coverage = np.where(population > 0, walking["population_with_access_data"] / population, np.nan)
    out = pd.DataFrame({
        "shapeID": lgas["shapeID"],
        "shapeName": lgas["shapeName"],
        "population_year": year,
        "population": np.round(population).astype("int64"),
        "motorised_minutes": motorised["mean_minutes"].round(1),
        "walking_minutes": walking["mean_minutes"].round(1),
        "walking_over_60min_share": walking["over_threshold_share"].round(4),
        "access_population_share": np.clip(coverage, 0, 1).round(4),
    })
    return out


def check(table: pd.DataFrame) -> None:
    """Refuse a table that breaks the guarantees the loader and dbt tests rely on."""
    if len(table) != EXPECTED_LGAS or table["shapeID"].nunique() != EXPECTED_LGAS:
        raise SourceError(f"expected {EXPECTED_LGAS} unique LGAs, got {len(table)}")
    if (table["population"] <= 0).any():
        bad = table.loc[table["population"] <= 0, "shapeName"].tolist()[:5]
        raise SourceError(f"LGAs with no population (a boundary or raster mismatch): {bad}")
    for col in ("motorised_minutes", "walking_minutes"):
        if (table[col].dropna() < 0).any():
            raise SourceError(f"{col} has negative minutes")
    for col in ("walking_over_60min_share", "access_population_share"):
        values = table[col].dropna()
        if ((values < 0) | (values > 1)).any():
            raise SourceError(f"{col} falls outside 0-1")
