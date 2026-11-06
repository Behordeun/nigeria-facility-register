"""
Build the vendored LGA-boundary artifact from geoBoundaries ADM2.

The published marts key on (state, canonical_lga) and the frame carries the
geoBoundaries shapeID, but no polygon geometry. A choropleth needs polygons, so
this vendors a simplified boundary set joined to the frame on shapeID.

The source is geoBoundaries Nigeria ADM2 (CC BY 4.0), which permits
redistribution with attribution, so the output is committed, not fetched. It is
simplified on the shared TOPOLOGY, not polygon by polygon: adjacent LGAs share a
border, and simplifying each polygon independently moves the two copies of a
shared edge differently, leaving slivers and gaps between neighbours. topojson
builds the arcs once, simplifies each arc once, and rebuilds the polygons, so a
shared border stays a single simplified line and neighbours keep matching edges.
This keeps the committed file around 1 MB instead of the ~9 MB raw geojson;
LGA-resolution choropleths do not need full-precision coastlines. The tolerance
is recorded in data/sources.yml so the artifact is reproducible from the source.

This script is a one-off regeneration tool, not part of the pipeline: the output
is committed and read directly. Re-run it only to refresh the boundaries or
change the tolerance. It needs the `topojson` package (pip install topojson).

Usage:
    python scripts/build_lga_geometry.py \
        --source /path/to/nigeria_adm2_lga_boundaries.geojson
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRAME = ROOT / "data" / "frame"
DEFAULT_OUT = FRAME / "nigeria_774_lga_boundaries.simplified.geojson"
FRAME_INDEX = FRAME / "nigeria_master_lga_index.csv"
SIMPLIFY_TOLERANCE = 0.01  # degrees; shared-arc (topology) tolerance, in sources.yml
EXPECTED_LGAS = 774
KEEP_COLUMNS = ["shapeID", "shapeName"]
# After exploding MultiPolygons the output is single Polygons only. The input to
# the simplify step may still be either, so both are accepted before the explode.
PRE_EXPLODE_POLYGON_TYPES = {"Polygon", "MultiPolygon"}


def _frame_shape_ids() -> set[str]:
    # `is not None`, not truthiness: a blank shapeID ("") is a corrupt frame row,
    # not an absent key, and must stay in the set so the 774/774 join check can
    # catch it rather than silently dropping it.
    with open(FRAME_INDEX) as fh:
        return {row["shapeID"] for row in csv.DictReader(fh) if row.get("shapeID") is not None}


def build(source: Path, out: Path, tolerance: float) -> None:
    import geopandas as gpd
    import topojson as tp

    source = source.resolve()
    out = out.resolve()
    if source == out:
        raise SystemExit(
            f"refusing in-place build: --source and --out are the same file ({out}); "
            "the source would be overwritten with only the simplified columns"
        )

    gdf = gpd.read_file(source)
    if len(gdf) != EXPECTED_LGAS:
        raise SystemExit(
            f"expected {EXPECTED_LGAS} boundary features, source has {len(gdf)}"
        )
    if missing_cols := [c for c in KEEP_COLUMNS if c not in gdf.columns]:
        raise SystemExit(f"source is missing required columns: {missing_cols}")

    # Every boundary must join the frame on shapeID, or the choropleth would drop
    # or mislabel LGAs. Check before writing, so a mismatched source fails here
    # rather than silently shipping a partial map.
    frame_ids = _frame_shape_ids()
    src_ids = set(gdf["shapeID"])
    only_source = src_ids - frame_ids
    only_frame = frame_ids - src_ids
    if only_source or only_frame:
        raise SystemExit(
            f"shapeID mismatch with the frame: {len(only_source)} only in source, "
            f"{len(only_frame)} only in frame; expected an exact 774/774 match"
        )

    # Topology-aware simplify: build arcs once, simplify shared arcs once, rebuild
    # polygons, so adjacent LGAs keep matching borders (no slivers or gaps).
    base = gdf[KEEP_COLUMNS + ["geometry"]].copy()
    topo = tp.Topology(base, prequantize=False)
    simplified = topo.toposimplify(tolerance).to_gdf()[KEEP_COLUMNS + ["geometry"]]

    # Reject anything that cannot be drawn as an LGA area: an empty geometry or a
    # non-polygonal one (a line/point) would leave a blank LGA on the map, and
    # is_valid alone accepts both. Polygon and MultiPolygon are both fine here;
    # the explode below reduces MultiPolygons to single Polygons.
    bad_type = ~simplified.geometry.geom_type.isin(PRE_EXPLODE_POLYGON_TYPES)
    empty = simplified.geometry.is_empty
    invalid = ~simplified.geometry.is_valid
    if bad_type.any() or empty.any() or invalid.any():
        raise SystemExit(
            f"after simplify: {int(bad_type.sum())} non-polygonal, {int(empty.sum())} empty, "
            f"{int(invalid.sum())} invalid geometries; adjust the tolerance"
        )
    if simplified["shapeID"].nunique() != EXPECTED_LGAS:
        raise SystemExit(
            f"simplify changed the LGA count to {simplified['shapeID'].nunique()}; "
            f"expected {EXPECTED_LGAS}"
        )

    # Explode MultiPolygons into one Polygon feature per part. A deck.gl polygon
    # layer rejects a MultiPolygon ("invalid polygon"), so a coastal or riverine
    # LGA with islands is split into several single-Polygon features that share
    # its shapeID and shapeName. The output grain becomes one feature per polygon
    # part; the 774 LGAs are preserved as distinct shapeIDs, the multipart ones
    # spanning more than one feature. index_parts is dropped so the output
    # carries no part index, only the kept columns.
    exploded = simplified.explode(index_parts=False).reset_index(drop=True)
    still_multi = ~exploded.geometry.geom_type.eq("Polygon")
    if still_multi.any():
        raise SystemExit(
            f"after explode: {int(still_multi.sum())} features are not single Polygons"
        )
    if exploded["shapeID"].nunique() != EXPECTED_LGAS:
        raise SystemExit(
            f"explode changed the LGA count to {exploded['shapeID'].nunique()}; "
            f"expected {EXPECTED_LGAS}"
        )
    simplified = exploded

    # Serialize with GeopandasGeoDataFrame.to_json (RFC 7946): this emits a plain
    # FeatureCollection with no driver-specific "name" member, so the artifact
    # never leaks a temp filename and the output is deterministic. Write to a
    # temp file in the destination directory, then atomically replace the
    # destination, so a failed or interrupted write never destroys the existing
    # committed artifact and a reader never sees a partial file.
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = simplified.to_json(drop_id=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=f".{out.name}.", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
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
