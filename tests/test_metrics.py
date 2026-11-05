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


def _seed_raw_v2(con: duckdb.DuckDBPyConnection, states: list[str]) -> None:
    con.execute("create table grid3_v2 (state varchar)")
    if states:
        con.executemany("insert into grid3_v2 values (?)", [(s,) for s in states])


def _seed_raw_v3(con: duckdb.DuckDBPyConnection, states: list[str]) -> None:
    con.execute("create table grid3_v3 (state_standard varchar)")
    if states:
        con.executemany("insert into grid3_v3 values (?)", [(s,) for s in states])


def test_carryover_rate_is_carried_over_the_expected_v2_population():
    con = _con()
    # 3 v2 facilities in a v3-covered state (Bayelsa) are the denominator; the
    # one v2 in Lagos (not v3-covered) is not comparable and excluded.
    _seed_raw_v2(con, ["Bayelsa", "Bayelsa", "Bayelsa", "Lagos"])
    _seed_raw_v3(con, ["Bayelsa"])
    _seed_lineage(con, [("a", "carried-over"), ("b", "carried-over"), ("c", "dropped")])
    # 2 carried / 3 expected v2 in covered states
    assert metrics.carryover_rate(con) == pytest.approx(2 / 3)


def test_carryover_rate_falls_when_a_facility_is_filtered_out_of_lineage():
    con = _con()
    # 4 v2 facilities could carry; only 2 reached the lineage as carried-over
    # (the other two lost their id/coords and never entered the model). The rate
    # must reflect the loss, not read back a misleading high value from lineage.
    _seed_raw_v2(con, ["Bayelsa"] * 4)
    _seed_raw_v3(con, ["Bayelsa"])
    _seed_lineage(con, [("a", "carried-over"), ("b", "carried-over")])
    assert metrics.carryover_rate(con) == pytest.approx(0.5)


def test_carryover_rate_is_none_when_no_expected_v2_facilities():
    con = _con()
    # v2 facilities exist but none in a v3-covered state: denominator zero.
    _seed_raw_v2(con, ["Lagos", "Kano"])
    _seed_raw_v3(con, ["Bayelsa"])
    _seed_lineage(con, [("a", "appeared")])
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


def test_coverage_resolve_rate_matches_fct_across_spellings():
    con = _con()
    # Authority carries the normalised 'FCT'; the coverage mart carries the
    # frame's 'Federal Capital Territory'. Both must normalise to FCT so the
    # FCT LGAs are counted, not silently dropped from both sides.
    _seed_authority(con, ["FCT"])
    _seed_coverage(
        con,
        [("Federal Capital Territory", "current"),
         ("Federal Capital Territory", "unmatched")],
    )
    assert metrics.coverage_lga_resolve_rate(con) == pytest.approx(0.5)


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


def test_raw_counts_reads_only_raw_tables():
    con = _con()
    # No staging tables exist: raw_counts must still succeed (pre-dbt path).
    _seed_raw_v2(con, ["Bayelsa", "Bayelsa"])
    _seed_raw_v3(con, ["Bayelsa"])
    assert metrics.raw_counts(con) == {"raw_v2_rows": 2, "raw_v3_rows": 1}


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
    _seed_raw_v2(con, ["Ekiti", "Ekiti"])
    _seed_raw_v3(con, ["Ekiti"])
    _seed_authority(con, ["Ekiti"])
    _seed_lineage(con, [("a", "carried-over"), ("b", "dropped")])
    _seed_coverage(con, [("Ekiti", "current")])
    _seed_drift(con, ["Ado-Ekiti"])
    m = metrics.collect(con)
    assert m.raw_v2_rows == 2
    assert m.raw_v3_rows == 1
    assert m.authority_rows == 1
    assert m.lineage_rows == 2
    # 1 carried-over of 2 expected v2 facilities in the covered state
    assert m.carryover_rate == pytest.approx(0.5)
    assert m.coverage_lga_resolve_rate == pytest.approx(1.0)
    assert m.drift_lga_resolve_rate == pytest.approx(1.0)
