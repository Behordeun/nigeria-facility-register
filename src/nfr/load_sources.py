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
import pandas as pd


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
    # Parse with pandas (deterministic comma CSV) rather than DuckDB's sniffer,
    # which can misread a file whose free-text column confuses delimiter
    # detection and collapse every row into a single column, silently breaking
    # downstream column references.
    df = pd.read_csv(path)
    con.execute(f"drop table if exists {table}")
    con.register("_csv_tmp", df)
    con.execute(f"create table {table} as select * from _csv_tmp")
    con.unregister("_csv_tmp")
    return con.execute(f"select count(*) from {table}").fetchone()[0]


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _safe_dir(raw: str) -> Path:
    """Resolve a CLI-supplied directory and confirm it stays within the project.

    The source directories come from argv, so a caller could pass an absolute or
    ../-laden path. Resolving and checking containment keeps a stray or hostile
    argument from pointing the loader at files outside the project tree.
    """
    p = Path(raw).resolve()
    if p != PROJECT_ROOT and PROJECT_ROOT not in p.parents:
        raise SystemExit(f"path {p} is outside the project root {PROJECT_ROOT}")
    return p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--raw", default="data/raw", help="dir holding the fetched geopackages")
    ap.add_argument("--frame", default="data/frame")
    ap.add_argument("--derived", default="data/derived")
    args = ap.parse_args()

    raw = _safe_dir(args.raw)
    frame = _safe_dir(args.frame)
    derived = _safe_dir(args.derived)
    db_path = Path(args.db).resolve()
    if PROJECT_ROOT not in db_path.parents:
        raise SystemExit(f"--db {db_path} is outside the project root {PROJECT_ROOT}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))

    jobs = [
        ("grid3_v2", raw / "nga_health_facilities_v2_0.gpkg", load_gpkg),
        ("grid3_v3", raw / "grid3_nga_health_facilities_v3_0.gpkg", load_gpkg),
        ("frame_master_index", frame / "nigeria_master_lga_index.csv", load_csv),
        ("lga_alias_crosswalk", frame / "lga_alias_crosswalk.csv", load_csv),
        ("ntd_sth_state", derived / "ntd_sth_state.csv", load_csv),
    ]
    for table, path, fn in jobs:
        if not path.exists():
            raise SystemExit(f"missing input for {table}: {path}\nrun scripts/fetch_sources.py first")
        n = fn(con, table, path)
        print(f"loaded {table:22s} {n:>7} rows  sha={sha256(path)}  from {path}")

    con.close()


if __name__ == "__main__":
    main()
