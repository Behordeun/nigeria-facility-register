"""
Build data/derived/malaria_lga.csv: per-LGA malaria parasite rate and clinical incidence.

Fetches two Malaria Atlas Project Nigeria cut-outs for one year into
data/raw/rasters/ (gitignored; never committed), each pinned by SHA-256 in
data/sources.yml (malaria_lga.upstream), and weights them by the WorldPop R2025A
population for the same year, which build_access_population.py already pins and
fetches. A download that does not match its pin is refused, so a silently changed
upstream file can never feed the build. The derivation is nfr.enrichment.malaria_burden;
see its docstring for what each column means.

Usage:
    python scripts/build_malaria_burden.py \
        --boundaries /path/to/Geospatial/_shared_frame/nigeria_adm2_lga_boundaries.geojson
    python scripts/build_malaria_burden.py --print-hashes   # record pins: fetch and print, no build
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from nfr.enrichment import access_population as ap  # noqa: E402
from nfr.enrichment import malaria_burden as mb  # noqa: E402
from nfr.files import write_csv_atomic  # noqa: E402

FRAME_INDEX = ROOT / "data" / "frame" / "nigeria_master_lga_index.csv"
SIMPLIFIED = ROOT / "data" / "frame" / "nigeria_774_lga_boundaries.simplified.geojson"
SOURCES = ROOT / "data" / "sources.yml"
RAW_DIR = ROOT / "data" / "raw" / "rasters"
OUT = ROOT / "data" / "derived" / "malaria_lga.csv"


def pins() -> tuple[dict[str, str | None], str | None]:
    """The two MAP malaria pins, and the WorldPop pin for the malaria year."""
    sources = (yaml.safe_load(SOURCES.read_text()) or {}).get("sources", {})
    malaria = (sources.get("malaria_lga", {}) or {}).get("upstream", {}) or {}
    worldpop = ((sources.get("access_population_lga", {}) or {}).get("upstream", {}) or {}).get("worldpop", {}) or {}
    maps = {key: (malaria.get(f"map_pf_{key}") or {}).get("sha256") for key in mb.MAP_MALARIA_COVERAGES}
    return maps, (worldpop.get("sha256_by_year") or {}).get(mb.MALARIA_YEAR)




def main() -> None:
    parser = argparse.ArgumentParser(description="Build per-LGA malaria burden from the Malaria Atlas Project.")
    parser.add_argument("--boundaries", type=Path, default=SIMPLIFIED, help="LGA polygons with a shapeID column")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--print-hashes", action="store_true",
                        help="ignore the pins, fetch, print the hashes to record in data/sources.yml, and stop")
    args = parser.parse_args()

    if args.boundaries.resolve() == SIMPLIFIED.resolve():
        print("warning: using the simplified boundaries; pass --boundaries for the committed build", file=sys.stderr)
    lgas = ap.load_lgas(args.boundaries, FRAME_INDEX)
    ap.check_within_bbox(lgas)
    map_pins, worldpop_pin = pins()
    if not args.print_hashes and (missing := [k for k, v in map_pins.items() if not v]):
        raise SystemExit(f"no pin for {missing}; record them with --print-hashes first")
    # Re-pinning mode only prints the MAP hashes and never reads the population
    # raster, so it needs no WorldPop pin; a build does, and fails before fetching.
    if not args.print_hashes and not worldpop_pin:
        raise SystemExit(f"no WorldPop pin for {mb.MALARIA_YEAR} under access_population_lga in data/sources.yml")

    year = mb.MALARIA_YEAR
    paths = {key: args.raw_dir / f"map_pf_{key}_{year}_202608_nga.tif" for key in mb.MAP_MALARIA_COVERAGES}
    hashes = {
        f"map_pf_{key}": ap.fetch(
            mb.malaria_coverage_url(coverage, year), paths[key], None if args.print_hashes else map_pins[key])
        for key, coverage in mb.MAP_MALARIA_COVERAGES.items()
    }
    if args.print_hashes:
        for key, value in hashes.items():
            print(f"{key}: {value}")
        print("record these under malaria_lga.upstream in data/sources.yml, then rerun without --print-hashes")
        return

    population = args.raw_dir / f"nga_pop_{year}_CN_100m_R2025A_v1.tif"
    ap.fetch(ap.WORLDPOP_URL.format(year=year), population, worldpop_pin)
    table = mb.derive(paths["pfpr"], paths["incidence"], population, lgas, year)
    mb.check(table)
    write_csv_atomic(table, args.out)
    print(f"wrote {args.out} ({len(table)} rows, malaria year {year}); sha256 {ap.sha256(args.out)}")


if __name__ == "__main__":
    main()
