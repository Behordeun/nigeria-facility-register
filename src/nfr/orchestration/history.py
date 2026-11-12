"""
Per-LGA history across published registers (`lga_history`).

Each refresh builds the register from scratch, so a trend ("did coverage in this
LGA improve?") needs the earlier vintages carried forward. At publish time, the
same step that records `register_changes` copies the live register's history
into the staged build and appends this build's figures, one row per (vintage,
LGA). The history therefore travels inside the register: a rollback restores an
older register with the history it had, and the next publish carries that
forward.

A vintage is the input fingerprint. Publishing the same fingerprint again (the
weekly refresh when nothing changed) adds no rows, so the history grows only
when the register's inputs do: 774 rows per new vintage.

The live register's history is read-only here. A live register from before
this table existed contributes its own figures as the first vintage, when it
has the columns needed; a register restored from such an archive is given its
own figures by ensure_history.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from nfr.orchestration import publish

TABLE = "lga_history"
# The published table's columns and types, in order. The bundle test reads this
# in place of a dbt contract, because this table is not built by dbt.
COLUMNS = {
    "built_at": "TIMESTAMP",
    "fingerprint": "VARCHAR",
    "state": "VARCHAR",
    "canonical_lga": "VARCHAR",
    "lga_label": "VARCHAR",
    "facilities": "BIGINT",
    "facilities_per_10k_population": "DOUBLE",
    "priority_score": "INTEGER",
    "priority_tier": "VARCHAR",
}
# What a register needs to contribute its own figures as a vintage.
_MART_COLUMNS = ("state", "canonical_lga", "lga_label", "facilities", "facilities_per_10k_population",
                 "priority_score", "priority_tier")

_CREATE = (
    'create or replace table "lga_history" (built_at timestamp not null, fingerprint varchar not null, '
    "state varchar not null, canonical_lga varchar not null, lga_label varchar not null, facilities bigint, "
    "facilities_per_10k_population double, priority_score integer, priority_tier varchar)"
)
# A register's own vintage: its coverage mart, stamped with its build time and
# input fingerprint.
_OWN_VINTAGE = (
    "select (select built_at from register_summary) as built_at, ?::varchar as fingerprint, "
    "state, canonical_lga, lga_label, facilities, facilities_per_10k_population, "
    "priority_score::integer as priority_score, priority_tier from lga_coverage_burden"
)
_SELECT_HISTORY = (
    "select built_at, fingerprint, state, canonical_lga, lga_label, facilities, "
    "facilities_per_10k_population, priority_score, priority_tier from lga_history"
)
_TABLE_COLUMNS = "select column_name from information_schema.columns where table_name = ?"


def write_lga_history(staging_db: Path, live_db: Path, current_fingerprint: str) -> None:
    """Write `lga_history` into the staging database: the live register's history plus this build.

    Raises if the staging database cannot be written, which fails the publish
    before promotion. A missing or older live register is not an error: the
    history then starts with what can be carried, or with this build alone.
    """
    carried = _carried_history(live_db)
    con = duckdb.connect(str(staging_db))
    try:
        con.execute("begin")
        con.execute(_CREATE)
        if carried is not None and len(carried):
            con.register("carried_history", carried)
            con.execute('insert into "lga_history" select * from carried_history')
            con.unregister("carried_history")
        already = con.execute('select count(*) from "lga_history" where fingerprint = ?',
                              [current_fingerprint]).fetchone()[0]
        if not already:
            con.execute(f'insert into "lga_history" {_OWN_VINTAGE}', [current_fingerprint])
        con.execute("commit")
    finally:
        con.close()


def ensure_history(db: Path) -> bool:
    """Give a register without `lga_history` its own vintage as its history; return whether one was added.

    Used on a restored copy before it goes live. A register that has the table
    keeps its own history; one that lacks the columns to describe itself gets an
    empty table, so the dashboard always finds it.
    """
    fingerprint = publish.read_published_fingerprint(str(db))
    con = duckdb.connect(str(db))
    try:
        if con.execute(_TABLE_COLUMNS, [TABLE]).fetchall():
            return False
        describable = fingerprint is not None and _has_own_vintage(con)
        con.execute("begin")
        con.execute(_CREATE)
        if describable:
            con.execute(f'insert into "lga_history" {_OWN_VINTAGE}', [fingerprint])
        con.execute("commit")
    finally:
        con.close()
    return True


def _carried_history(live_db: Path):
    """The live register's history as a data frame, or its own vintage, or None."""
    if not live_db.is_file():
        return None
    fingerprint = publish.read_published_fingerprint(str(live_db))
    con = duckdb.connect(str(live_db), read_only=True)
    try:
        if set(COLUMNS) <= {row[0] for row in con.execute(_TABLE_COLUMNS, [TABLE]).fetchall()}:
            return con.execute(_SELECT_HISTORY).df()
        if fingerprint is not None and _has_own_vintage(con):
            return con.execute(_OWN_VINTAGE, [fingerprint]).df()
        return None
    finally:
        con.close()


def _has_own_vintage(con: duckdb.DuckDBPyConnection) -> bool:
    mart = {row[0] for row in con.execute(_TABLE_COLUMNS, ["lga_coverage_burden"]).fetchall()}
    summary = {row[0] for row in con.execute(_TABLE_COLUMNS, ["register_summary"]).fetchall()}
    return set(_MART_COLUMNS) <= mart and "built_at" in summary
