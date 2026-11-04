"""
Load source inputs into DuckDB as the `raw` schema dbt reads from.

Each table is dropped and recreated wholesale from its source file, never edited
in place, so a reload is idempotent and safe to retry. The authoritative copies
live upstream (HDX, ESPEN); these tables are only the local read surface. Source
files are fetched separately (see scripts/fetch_sources.py) and their provenance
is pinned in data/sources.yml.

Usage:
    python -m nfr.load_sources --db build/nfr.duckdb --frame data/frame
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import duckdb
import geopandas as gpd


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def load_gpkg(con: duckdb.DuckDBPyConnection, table: str, path: Path) -> int:
    gdf = gpd.read_file(path)
    df = gdf.drop(columns=[c for c in ["geometry"] if c in gdf.columns])
    con.execute(f"drop table if exists {table}")
    con.register("_tmp", df)
    con.execute(f"create table {table} as select * from _tmp")
    con.unregister("_tmp")
    return len(df)


def load_csv(con: duckdb.DuckDBPyConnection, table: str, path: Path) -> int:
    con.execute(f"drop table if exists {table}")
    con.execute(
        f"create table {table} as "
        f"select * from read_csv_auto(?, header=true, all_varchar=false)",
        [str(path)],
    )
    return con.execute(f"select count(*) from {table}").fetchone()[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--raw", default="data/raw", help="dir holding the fetched geopackages")
    ap.add_argument("--frame", default="data/frame")
    ap.add_argument("--derived", default="data/derived")
    args = ap.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(args.db)

    jobs = [
        ("grid3_v2", Path(args.raw) / "nga_health_facilities_v2_0.gpkg", load_gpkg),
        ("grid3_v3", Path(args.raw) / "grid3_nga_health_facilities_v3_0.gpkg", load_gpkg),
        ("frame_master_index", Path(args.frame) / "nigeria_master_lga_index.csv", load_csv),
        ("ntd_sth_state", Path(args.derived) / "ntd_sth_state.csv", load_csv),
    ]
    for table, path, fn in jobs:
        if not path.exists():
            raise SystemExit(f"missing input for {table}: {path}\nrun scripts/fetch_sources.py first")
        n = fn(con, table, path)
        print(f"loaded {table:22s} {n:>7} rows  sha={sha256(path)}  from {path}")

    con.close()


if __name__ == "__main__":
    main()
