"""
Load source inputs into DuckDB as the `raw` schema dbt reads from.

Each table is dropped and recreated wholesale from its source file, never edited
in place. All inputs are validated and parsed first, then the tables are
replaced inside a single transaction, so a missing input or an interruption
cannot leave dbt reading a mixture of refreshed and stale tables. The
authoritative copies live upstream (HDX, ESPEN); these tables are only the local
read surface. Source files are fetched separately (see scripts/fetch_sources.py)
and their provenance is pinned in data/sources.yml.

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

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The only table names the loader will ever create. Table names are validated
# against this set before being used as SQL identifiers, so a name can never
# carry injected SQL regardless of where it came from.
ALLOWED_TABLES = {
    "grid3_v2",
    "grid3_v3",
    "frame_master_index",
    "lga_alias_crosswalk",
    "ntd_sth_state",
    "ntd_espen_lga",
    "flood_event_affected_lga",
    "births_lga",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _quoted_ident(table: str) -> str:
    """Return a safe double-quoted SQL identifier for a known table name.

    Only names on the allowlist are accepted, so the identifier interpolated
    into the CREATE/DROP statements can never carry injected SQL.
    """
    if table not in ALLOWED_TABLES:
        raise ValueError(f"unknown table name: {table!r}")
    return '"' + table.replace('"', '""') + '"'


def read_gpkg(path: Path) -> pd.DataFrame:
    # Open the resolved path that was checked, not the caller's spelling of it,
    # so what is read is exactly what passed the project-root check.
    real = Path(path).resolve()
    if not real.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"path {path} resolves outside the project root")
    gdf = gpd.read_file(real)
    return gdf.drop(columns=[c for c in ["geometry"] if c in gdf.columns])


def read_csv(path: Path) -> pd.DataFrame:
    # Parse with pandas (deterministic comma CSV) rather than DuckDB's sniffer,
    # which can misread a file whose free-text column confuses delimiter
    # detection and collapse every row into a single column, silently breaking
    # downstream column references.
    return pd.read_csv(path)


def _replace_table(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> int:
    ident = _quoted_ident(table)
    reg = f"_load_{table}"
    con.register(reg, df)
    con.execute(f"drop table if exists {ident}")
    con.execute(f"create table {ident} as select * from {reg}")
    con.unregister(reg)
    return len(df)


def _safe_input_file(path: Path) -> Path:
    """Fully resolve an input file and confirm it stays within the project.

    Resolves symlinks as well as `..`, so a file inside an allowed directory
    that is a symlink to somewhere outside the project tree is rejected before
    the loader opens it. Returns the real path.
    """
    real = path.resolve()
    if not real.is_relative_to(PROJECT_ROOT):
        raise SystemExit(f"input {real} resolves outside the project root {PROJECT_ROOT}")
    if not real.is_file():
        raise SystemExit(f"missing input: {path}\nrun scripts/fetch_sources.py first")
    return real


def _safe_dir(raw: str) -> Path:
    """Resolve a CLI-supplied directory and confirm it stays within the project."""
    p = Path(raw).resolve()
    if not p.is_relative_to(PROJECT_ROOT):
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

    # The output database path is not restricted to the project root: a caller
    # may legitimately point --db at a scratch or external location. Source
    # inputs stay confined to the project.
    db_path = Path(args.db).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    jobs = [
        ("grid3_v2", raw / "nga_health_facilities_v2_0.gpkg", read_gpkg),
        ("grid3_v3", raw / "grid3_nga_health_facilities_v3_0.gpkg", read_gpkg),
        ("frame_master_index", frame / "nigeria_master_lga_index.csv", read_csv),
        ("lga_alias_crosswalk", frame / "lga_alias_crosswalk.csv", read_csv),
        ("ntd_sth_state", derived / "ntd_sth_state.csv", read_csv),
        # Enrichment inputs (derived per-LGA tables built by
        # scripts/build_enrichment_sources.py). ntd_espen_lga feeds the coverage
        # mart through stg_ntd_endemicity_lga; the other two are loaded and
        # tested only.
        ("ntd_espen_lga", derived / "ntd_espen_lga.csv", read_csv),
        ("flood_event_affected_lga", derived / "flood_event_affected_lga.csv", read_csv),
        ("births_lga", derived / "births_lga.csv", read_csv),
    ]

    # Validate and read every input BEFORE touching the database, so a missing
    # or bad input aborts without having replaced any table.
    staged: list[tuple[str, pd.DataFrame, Path, str]] = []
    for table, path, reader in jobs:
        real = _safe_input_file(path)
        df = reader(real)
        staged.append((table, df, real, sha256(real)))

    con = duckdb.connect(str(db_path))
    try:
        # Replace all tables in one transaction: either every table is the new
        # vintage or, on failure, none are changed.
        con.execute("begin transaction")
        for table, df, real, digest in staged:
            n = _replace_table(con, table, df)
            print(f"loaded {table:22s} {n:>7} rows  sha={digest}  from {real}")
        con.execute("commit")
    except Exception:
        con.execute("rollback")
        raise
    finally:
        con.close()


if __name__ == "__main__":
    main()
