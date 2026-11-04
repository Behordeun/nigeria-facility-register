"""
Fetch the raw inputs listed in data/sources.yml into data/raw/.

The GRID3 geopackages sit behind an AWS WAF JavaScript challenge on HDX, so a
plain scripted GET returns a 202 challenge page, not the file. This script does
not try to defeat that. It prints the exact dataset pages and the target
filenames, and verifies any file already present against the manifest. Download
the two .gpkg resources in a browser (the WAF passes for a human) and drop them
into data/raw/ with the names below, then re-run to verify.

Usage:
    python scripts/fetch_sources.py            # check what is present
    python scripts/fetch_sources.py --verify   # fail if anything is missing
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

EXPECTED = {
    "nga_health_facilities_v2_0.gpkg":
        "https://data.humdata.org/dataset/grid3-nga-health-facilities-v2-0",
    "grid3_nga_health_facilities_v3_0.gpkg":
        "https://data.humdata.org/dataset/grid3-nga-health-facilities-v3-0",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true",
                    help="exit non-zero if any required raw file is missing")
    args = ap.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    missing = []
    for name, page in EXPECTED.items():
        p = RAW / name
        if p.exists() and p.stat().st_size > 1_000_000:
            print(f"present: {name} ({p.stat().st_size // 1_000_000} MB)")
        else:
            missing.append(name)
            print(f"MISSING: {name}")
            print(f"         download the .gpkg resource from {page}")
            print(f"         save it to {p}")

    if missing and args.verify:
        print(f"\n{len(missing)} raw file(s) missing. HDX serves these behind an AWS "
              "WAF that blocks scripted download; fetch them in a browser.")
        sys.exit(1)
    if not missing:
        print("\nall raw inputs present.")


if __name__ == "__main__":
    main()
