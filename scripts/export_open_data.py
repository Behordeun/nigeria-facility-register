"""
Export the published register as the open data release for a release tag.

Writes two zip archives: the CC BY 4.0 set (tables as Parquet and CSV, and the
LGA boundaries as a GeoPackage) and the non-commercial set (the ESPEN-derived
columns). See nfr/open_data.py for how columns are split.

Usage:
    python scripts/export_open_data.py --label v0.14.0 [--db PATH] [--out DIR]

In the compose deployment the register lives in the daemon's build volume, so
run it there; the archives land in build/open_data/:
    docker compose exec -T daemon uv run python scripts/export_open_data.py --label v0.14.0
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from nfr import open_data  # noqa: E402
from nfr.orchestration import config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", required=True, help="the release tag the files are attached to, e.g. v0.14.0")
    parser.add_argument("--db", type=Path, default=config.DB, help=f"the register to export (default {config.DB})")
    parser.add_argument("--out", type=Path, default=config.BUILD_DIR / "open_data",
                        help="where the archives are written (default build/open_data)")
    args = parser.parse_args()
    try:
        archives = open_data.export_release(args.db, args.out, args.label, ROOT)
    except open_data.OpenDataError as exc:
        print(f"export refused: {exc}", file=sys.stderr)
        return 1
    for archive in archives:
        print(archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
