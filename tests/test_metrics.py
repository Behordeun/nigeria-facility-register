"""Unit tests for the refresh run metrics.

The metrics feed Dagster run metadata and the asset checks, so they must count
the right rows and, above all, return None rather than a misleading 0.0 when a
rate's denominator is zero. These build tiny in-memory tables so the queries are
tested without the sample fixture or a Dagster context. duckdb is a hard
dependency of the package, so these run in the base test suite.
"""
from __future__ import annotations

import duckdb
import pytest

from nfr.orchestration import metrics


def _con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


def _seed_lineage(con: duckdb.DuckDBPyConnection, rows: list[tuple[str, str]]) -> None:
    # rows: (facility_id, lineage_status)
    con.execute("create table stg_version_lineage (facility_id varchar, lineage_status varchar)")
    if rows:
        con.executemany("insert into stg_version_lineage values (?, ?)", rows)


def _seed_coverage(con: duckdb.DuckDBPyConnection, rows: list[tuple[str, str]]) -> None:
    # rows: (state, facility_status)
    con.execute("create table lga_coverage_burden (state varchar, facility_status varchar)")
    if rows:
        con.executemany("insert into lga_coverage_burden values (?, ?)", rows)


def _seed_authority(con: duckdb.DuckDBPyConnection, states: list[str]) -> None:
    con.execute("create table stg_facilities_authority (state varchar)")
    if states:
        con.executemany("insert into stg_facilities_authority values (?)", [(s,) for s in states])


def _seed_drift(con: duckdb.DuckDBPyConnection, canonical: list[str | None]) -> None:
    con.execute("create table lga_version_drift (canonical_lga varchar)")
    if canonical:
        con.executemany("insert into lga_version_drift values (?)", [(c,) for c in canonical])


def test_carryover_rate_is_carried_over_the_v2_facilities():
    con = _con()
    _seed_lineage(
        con,
        [("a", "carried-over"), ("b", "carried-over"), ("c", "carried-over"),
         ("d", "dropped"), ("e", "appeared")],
    )
    # 3 carried / (3 carried + 1 dropped); appeared is not in the denominator
    assert metrics.carryover_rate(con) == pytest.approx(0.75)


def test_carryover_rate_is_none_when_no_v2_facilities():
    con = _con()
    _seed_lineage(con, [("a", "appeared"), ("b", "appeared")])
    # no carried-over and no dropped: denominator zero, not a misleading 0.0
    assert metrics.carryover_rate(con) is None


def test_coverage_resolve_rate_scoped_to_loaded_states():
    con = _con()
    _seed_authority(con, ["Ekiti", "Bayelsa"])
    _seed_coverage(
        con,
        [("Ekiti", "current"), ("Ekiti", "unmatched"),
         ("Bayelsa", "current"),
         # An LGA in a state the vintage did not load must not dilute the rate.
         ("Lagos", "unmatched"), ("Lagos", "unmatched")],
    )
    # Among loaded states (Ekiti, Bayelsa): 2 resolved of 3.
    assert metrics.coverage_lga_resolve_rate(con) == pytest.approx(2 / 3)


def test_coverage_resolve_rate_is_none_when_no_loaded_lgas():
    con = _con()
    _seed_authority(con, [])
    _seed_coverage(con, [("Lagos", "unmatched")])
    assert metrics.coverage_lga_resolve_rate(con) is None


def test_drift_resolve_rate_counts_non_null_canonical():
    con = _con()
    _seed_drift(con, ["Ado-Ekiti", None, "Yenagoa", "Ikere", None])
    assert metrics.drift_lga_resolve_rate(con) == pytest.approx(3 / 5)


def test_drift_resolve_rate_is_none_when_empty():
    con = _con()
    _seed_drift(con, [])
    assert metrics.drift_lga_resolve_rate(con) is None


def test_load_counts_reads_each_table():
    con = _con()
    con.execute("create table grid3_v2 as select * from (values (1), (2), (3)) t(x)")
    con.execute("create table grid3_v3 as select * from (values (1), (2)) t(x)")
    con.execute("create table stg_facilities_authority as select * from (values (1)) t(x)")
    con.execute("create table stg_version_lineage as select * from (values (1), (2), (3), (4)) t(x)")
    counts = metrics.load_counts(con)
    assert counts == {
        "raw_v2_rows": 3,
        "raw_v3_rows": 2,
        "authority_rows": 1,
        "lineage_rows": 4,
    }


def test_collect_assembles_all_metrics():
    con = _con()
    con.execute("create table grid3_v2 as select * from (values (1)) t(x)")
    con.execute("create table grid3_v3 as select * from (values (1)) t(x)")
    _seed_authority(con, ["Ekiti"])
    _seed_lineage(con, [("a", "carried-over"), ("b", "dropped")])
    _seed_coverage(con, [("Ekiti", "current")])
    _seed_drift(con, ["Ado-Ekiti"])
    m = metrics.collect(con)
    assert m.raw_v2_rows == 1
    assert m.lineage_rows == 2
    assert m.carryover_rate == pytest.approx(0.5)
    assert m.coverage_lga_resolve_rate == pytest.approx(1.0)
    assert m.drift_lga_resolve_rate == pytest.approx(1.0)
