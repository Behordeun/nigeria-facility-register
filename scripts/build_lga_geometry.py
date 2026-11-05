"""
Build the vendored LGA-boundary artifact from geoBoundaries ADM2.

The published marts key on (state, canonical_lga) and the frame carries the
geoBoundaries shapeID, but no polygon geometry. A choropleth needs polygons, so
this vendors a simplified boundary set joined to the frame on shapeID.

The source is geoBoundaries Nigeria ADM2 (CC BY 4.0), which permits
redistribution with attribution, so the output is committed, not fetched. It is
simplified (Douglas-Peucker, topology-preserving) to keep the committed file
around 1-1.5 MB instead of the ~9 MB raw geojson; LGA-resolution choropleths do
not need full-precision coastlines. The simplification tolerance is recorded in
data/sources.yml so the artifact is reproducible from the source.

This script is a one-off regeneration tool, not part of the pipeline: the output
is committed and read directly. Re-run it only to refresh the boundaries or
change the tolerance.

Usage:
    python scripts/build_lga_geometry.py \
        --source /path/to/nigeria_adm2_lga_boundaries.geojson
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRAME = ROOT / "data" / "frame"
DEFAULT_OUT = FRAME / "nigeria_774_lga_boundaries.simplified.geojson"
FRAME_INDEX = FRAME / "nigeria_master_lga_index.csv"
SIMPLIFY_TOLERANCE = 0.005  # degrees; recorded in data/sources.yml
EXPECTED_FEATURES = 774
KEEP_COLUMNS = ["shapeID", "shapeName"]


def _frame_shape_ids() -> set[str]:
    import csv

    with open(FRAME_INDEX) as fh:
        return {row["shapeID"] for row in csv.DictReader(fh) if row.get("shapeID")}


def build(source: Path, out: Path, tolerance: float) -> None:
    import geopandas as gpd

    gdf = gpd.read_file(source)
    if len(gdf) != EXPECTED_FEATURES:
        raise SystemExit(
            f"expected {EXPECTED_FEATURES} boundary features, source has {len(gdf)}"
        )
    missing_cols = [c for c in KEEP_COLUMNS if c not in gdf.columns]
    if missing_cols:
        raise SystemExit(f"source is missing required columns: {missing_cols}")

    # Every boundary must join the frame on shapeID, or the choropleth would
    # drop or mislabel LGAs. Check before writing, so a mismatched source fails
    # here rather than silently shipping a partial map.
    frame_ids = _frame_shape_ids()
    src_ids = set(gdf["shapeID"])
    only_source = src_ids - frame_ids
    only_frame = frame_ids - src_ids
    if only_source or only_frame:
        raise SystemExit(
            f"shapeID mismatch with the frame: {len(only_source)} only in source, "
            f"{len(only_frame)} only in frame; expected an exact 774/774 match"
        )

    simplified = gdf[KEEP_COLUMNS + ["geometry"]].copy()
    simplified["geometry"] = simplified.geometry.simplify(
        tolerance, preserve_topology=True
    )
    if not simplified.geometry.is_valid.all():
        n_invalid = int((~simplified.geometry.is_valid).sum())
        raise SystemExit(f"{n_invalid} geometries invalid after simplify; lower the tolerance")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    simplified.to_file(out, driver="GeoJSON")
    size_kb = out.stat().st_size // 1024
    print(f"wrote {out} ({len(simplified)} features, {size_kb} KB, tol={tolerance})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--source", required=True,
        help="path to the geoBoundaries NGA ADM2 geojson (CC BY 4.0)",
    )
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--tolerance", type=float, default=SIMPLIFY_TOLERANCE)
    args = ap.parse_args()

    source = Path(args.source).expanduser()
    if not source.is_file():
        print(f"source not found: {source}", file=sys.stderr)
        sys.exit(1)
    build(source, Path(args.out), args.tolerance)


if __name__ == "__main__":
    main()
