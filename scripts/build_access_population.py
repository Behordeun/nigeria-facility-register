"""
Build data/derived/access_population_lga.csv: per-LGA population and access to healthcare.

Fetches public rasters into data/raw/rasters/ (gitignored; never committed):
the WorldPop R2025A Nigeria population for each population year, and the Malaria
Atlas Project motorised and walking-only travel time to healthcare, cut to
Nigeria's extent. The table holds one row per LGA per year for every year pinned
in data/sources.yml (access_population_lga.upstream.worldpop.sha256_by_year), so
the dbt build can roll forward to the current year without fetching anything.
Each fetch is skipped when the file on disk already matches its pinned SHA-256,
and a download that does not match its pin is refused, so a silently changed
upstream file can never feed the build. A year with no pin is refused too. The derivation is nfr.enrichment.access_population; see its docstring for
what each column means.

Boundaries: the committed table is built from the full-resolution geoBoundaries
ADM2 polygons (pass --boundaries). Without it, the vendored simplified polygons
are used, which shift LGA edges by up to about 1 km: fine for travel time, but it
misplaces population between small neighbouring urban LGAs, so the script warns.

Usage:
    python scripts/build_access_population.py \
        --boundaries /path/to/Geospatial/_shared_frame/nigeria_adm2_lga_boundaries.geojson
    python scripts/build_access_population.py --print-hashes --years 2031   # record pins for new years: fetch and print, no build
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from nfr.enrichment import access_population as ap  # noqa: E402

FRAME_INDEX = ROOT / "data" / "frame" / "nigeria_master_lga_index.csv"
SIMPLIFIED = ROOT / "data" / "frame" / "nigeria_774_lga_boundaries.simplified.geojson"
SOURCES = ROOT / "data" / "sources.yml"
RAW_DIR = ROOT / "data" / "raw" / "rasters"
OUT = ROOT / "data" / "derived" / "access_population_lga.csv"


def pins() -> tuple[dict[int, str], dict[str, str | None]]:
    """Upstream SHA-256 pins from data/sources.yml: WorldPop by year, and the two MAP rasters."""
    entry = (yaml.safe_load(SOURCES.read_text()) or {}).get("sources", {}).get("access_population_lga", {})
    upstream = entry.get("upstream", {}) or {}
    worldpop = {int(year): sha for year, sha in ((upstream.get("worldpop") or {}).get("sha256_by_year") or {}).items()}
    maps = {key: (upstream.get(key) or {}).get("sha256") for key in ("map_motorised", "map_walking")}
    return worldpop, maps


def parse_years(spec: str) -> list[int]:
    """'2025-2030' or '2025,2027' to a sorted list of years."""
    years: set[int] = set()
    for part in spec.split(","):
        start, _, end = part.strip().partition("-")
        years.update(range(int(start), int(end or start) + 1))
    return sorted(years)


def write_atomic(table, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=f".{out.name}.", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            table.to_csv(fh, index=False)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Build per-LGA population and access to healthcare.")
    parser.add_argument("--boundaries", type=Path, default=SIMPLIFIED, help="LGA polygons with a shapeID column")
    parser.add_argument("--years", type=parse_years, default=None,
                        help="population years, e.g. 2025-2030 (default: every year pinned in data/sources.yml)")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--print-hashes", action="store_true",
                        help="ignore the pins, fetch, print the hashes to record in data/sources.yml, and stop without building")
    args = parser.parse_args()

    if args.boundaries.resolve() == SIMPLIFIED.resolve():
        print("warning: using the simplified boundaries; pass --boundaries for the committed build", file=sys.stderr)
    lgas = ap.load_lgas(args.boundaries, FRAME_INDEX)
    ap.check_within_bbox(lgas)
    bbox = ap.NIGERIA_BBOX
    worldpop_pins, map_pins = pins()
    years = args.years or sorted(worldpop_pins)
    if not years:
        raise SystemExit("no population years: pin at least one in data/sources.yml or pass --years")
    if not args.print_hashes and (unpinned := [y for y in years if y not in worldpop_pins]):
        raise SystemExit(f"no WorldPop pin for {unpinned}; record one with --print-hashes --years first")

    def pin(value):
        # Re-pinning mode fetches without checking, to report what is upstream now.
        return None if args.print_hashes else value

    motorised = args.raw_dir / "map_motorised_travel_time_healthcare_2020_nga.tif"
    walking = args.raw_dir / "map_walking_travel_time_healthcare_2020_nga.tif"
    hashes = {
        "map_motorised": ap.fetch(ap.map_coverage_url(ap.MAP_COVERAGES["motorised"], bbox), motorised, pin(map_pins["map_motorised"])),
        "map_walking": ap.fetch(ap.map_coverage_url(ap.MAP_COVERAGES["walking"], bbox), walking, pin(map_pins["map_walking"])),
    }
    rasters = {}
    for year in years:
        rasters[year] = args.raw_dir / f"nga_pop_{year}_CN_100m_R2025A_v1.tif"
        hashes[f"worldpop {year}"] = ap.fetch(ap.WORLDPOP_URL.format(year=year), rasters[year], pin(worldpop_pins.get(year)))
    if args.print_hashes:
        # Re-pinning mode: report what is upstream now and stop. The table is
        # only ever built from files that match the pins in data/sources.yml.
        for key, value in hashes.items():
            print(f"{key}: {value}")
        print("record these under access_population_lga.upstream in data/sources.yml, then rerun without --print-hashes")
        return

    table = pd.concat(
        [ap.derive(rasters[year], motorised, walking, lgas, year) for year in years], ignore_index=True
    )
    ap.check(table)
    write_atomic(table, args.out)
    totals = table.groupby("population_year")["population"].sum()
    print(f"wrote {args.out} ({len(table)} rows; population "
          + ", ".join(f"{y}: {int(v):,}" for y, v in totals.items())
          + f"); sha256 {ap.sha256(args.out)}")


if __name__ == "__main__":
    main()
