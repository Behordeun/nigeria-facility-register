"""
Build data/derived/ntd_espen_lga.csv from ESPEN's public download route.

Downloads the five IU-level exports (STH, schistosomiasis, lymphatic filariasis,
onchocerciasis, Loa loa) for Nigeria, 2014 to the year pinned as latest_year
under ntd_espen_lga in data/sources.yml, into data/raw/espen/ (gitignored;
ESPEN's terms are non-commercial, so the exports are never committed). Maps each
IU to its LGA through the master LGA index and reduces them to one row per (LGA,
disease); see nfr.enrichment.espen.

The pin is on the committed table, not the downloads: ESPEN's exports also carry
mass-drug-administration fields that change between downloads without changing
any endemicity, so the build compares the table it derives with the sha256
recorded under ntd_espen_lga and refuses to overwrite the committed table when
they differ. A difference means ESPEN's endemicity data changed: review it, then
rerun with --update and record the new hash.

Usage:
    python scripts/build_espen_lga.py              # download, rebuild, check against the pin
    python scripts/build_espen_lga.py --update     # write a changed table; prints the hash to pin
    python scripts/build_espen_lga.py --refresh    # download again even if data/raw/espen/ has the files
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from nfr.enrichment import access_population as ap  # noqa: E402
from nfr.enrichment import espen  # noqa: E402
from nfr.files import write_csv_atomic  # noqa: E402

FRAME_INDEX = ROOT / "data" / "frame" / "nigeria_master_lga_index.csv"
SOURCES = ROOT / "data" / "sources.yml"
RAW_DIR = ROOT / "data" / "raw" / "espen"
OUT = ROOT / "data" / "derived" / "ntd_espen_lga.csv"
EXPECTED_LGAS = 774


def pinned() -> tuple[int, str | None]:
    """The latest ESPEN year to request and the committed table's pinned sha256."""
    entry = (yaml.safe_load(SOURCES.read_text()) or {}).get("sources", {}).get("ntd_espen_lga", {}) or {}
    if not entry.get("latest_year"):
        raise SystemExit("no latest_year under ntd_espen_lga in data/sources.yml")
    return int(entry["latest_year"]), entry.get("sha256")


def fetch_exports(raw_dir: Path, last_year: int, refresh: bool) -> dict[str, Path]:
    """Download each disease's export (or reuse it); return the paths."""
    paths = {}
    for code in espen.DISEASE_CODES:
        dest = raw_dir / f"espen_NG_{code}_iu_{espen.FIRST_YEAR}_{last_year}.csv"
        if refresh:
            dest.unlink(missing_ok=True)
        digest = ap.fetch(espen.download_url(code, last_year), dest)
        print(f"{code:6s} {dest.name}  sha256 {digest}")
        paths[code] = dest
    return paths


def build_table(paths: dict[str, Path]) -> pd.DataFrame:
    """The per-(LGA, disease) table from the downloaded exports."""
    exports = {code: pd.read_csv(path, low_memory=False) for code, path in paths.items()}
    master = pd.read_csv(FRAME_INDEX, dtype=str)
    table = espen.derive_espen(espen.long_from_exports(exports, master))
    if len(table) != EXPECTED_LGAS * len(espen.DISEASE_CODES):
        raise SystemExit(f"espen: expected {EXPECTED_LGAS} LGAs x {len(espen.DISEASE_CODES)} diseases, got {len(table)}")
    if table["canonical_lga"].isna().any() or table.duplicated(["state", "canonical_lga", "disease"]).any():
        raise SystemExit("espen: the table does not cover each LGA and disease exactly once")
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description="Build ntd_espen_lga.csv from ESPEN's public download route.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--update", action="store_true",
                        help="write the table even if it differs from the pinned sha256, and print the new hash")
    parser.add_argument("--refresh", action="store_true", help="download the exports again")
    args = parser.parse_args()

    last_year, pin = pinned()
    table = build_table(fetch_exports(args.raw_dir, last_year, args.refresh))

    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / args.out.name
        write_csv_atomic(table, candidate)
        digest = ap.sha256(candidate)
        if digest == pin:
            print(f"rebuilt {args.out.name} matches the pinned sha256 {digest}; the committed table is reproducible")
            if not args.out.is_file() or ap.sha256(args.out) != digest:
                write_csv_atomic(table, args.out)
                print(f"wrote {args.out}")
            return 0
        if not args.update:
            print(f"rebuilt table sha256 {digest} differs from the pin {pin}: ESPEN's endemicity data changed. "
                  "Review the difference, then rerun with --update and record the new hash.", file=sys.stderr)
            return 1
        write_csv_atomic(table, args.out)
        print(f"wrote {args.out} ({len(table)} rows); record sha256 {digest} under ntd_espen_lga in data/sources.yml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
