"""Unit tests for the data-quality gate thresholds.

The gate's blocking carry-over floor must sit below the advisory warn floor, or
the blocking check would fire before (or together with) the warning and the two
severities would collapse into one. dagster is an optional install, so skip when
it is absent (the definitions module imports dagster at the top).
"""
import pytest

pytest.importorskip("dagster")

from nfr.orchestration import definitions as d  # noqa: E402


def test_hard_carryover_floor_is_below_the_warn_floor():
    # The gate (blocking) must trip only on a genuine collapse, strictly below
    # the advisory warn floor, so a dip warns before it ever blocks.
    assert d.HARD_MIN_CARRYOVER_RATE < d.MIN_CARRYOVER_RATE


def test_floors_are_fractions():
    for floor in (d.MIN_LGA_RESOLVE_RATE, d.MIN_CARRYOVER_RATE, d.HARD_MIN_CARRYOVER_RATE):
        assert 0.0 < floor < 1.0


def _gate_db(tmp_path, *, v2_in_covered: int, carried: int):
    """Build a DuckDB file the gate reads: a v3-covered state with v2_in_covered
    v2 facilities and `carried` carried-over lineage rows, plus the two mart
    tables the gate drops on failure."""
    import duckdb

    p = tmp_path / "gate.duckdb"
    con = duckdb.connect(str(p))
    con.execute("create table grid3_v3 (state_standard varchar)")
    con.execute("insert into grid3_v3 values ('Bayelsa')")
    con.execute("create table grid3_v2 (state varchar)")
    if v2_in_covered:
        con.executemany("insert into grid3_v2 values (?)", [("Bayelsa",)] * v2_in_covered)
    con.execute("create table stg_version_lineage (facility_id varchar, lineage_status varchar)")
    if carried:
        con.executemany(
            "insert into stg_version_lineage values (?, 'carried-over')",
            [(f"f{i}",) for i in range(carried)],
        )
    con.execute("create table lga_coverage_burden (x int)")
    con.execute("create table lga_version_drift (x int)")
    con.close()
    return str(p)


def test_gate_passes_and_leaves_tables_when_rate_clears_floor(tmp_path):
    # 9 carried of 10 expected = 0.9, well above the hard floor.
    db = _gate_db(tmp_path, v2_in_covered=10, carried=9)
    rate = d._enforce_carryover_gate(db)
    assert rate == pytest.approx(0.9)
    import duckdb

    con = duckdb.connect(db, read_only=True)
    tables = {t[0] for t in con.sql("show tables").fetchall()}
    con.close()
    assert {"lga_coverage_burden", "lga_version_drift", "stg_version_lineage"} <= tables


def test_gate_fails_and_drops_marts_when_rate_below_floor(tmp_path):
    # 1 carried of 10 expected = 0.1, below the 0.30 hard floor.
    db = _gate_db(tmp_path, v2_in_covered=10, carried=1)
    with pytest.raises(RuntimeError, match="data-quality gate failed"):
        d._enforce_carryover_gate(db)
    import duckdb

    con = duckdb.connect(db, read_only=True)
    tables = {t[0] for t in con.sql("show tables").fetchall()}
    con.close()
    # The rejected marts and lineage are dropped so no reader sees them.
    assert not ({"lga_coverage_burden", "lga_version_drift", "stg_version_lineage"} & tables)


def test_gate_fails_when_rate_is_undefined(tmp_path):
    # No v2 facilities in a covered state: rate is None, which must fail the
    # gate rather than pass as "nothing to check".
    db = _gate_db(tmp_path, v2_in_covered=0, carried=0)
    with pytest.raises(RuntimeError, match="data-quality gate failed"):
        d._enforce_carryover_gate(db)


def test_gate_drop_statements_are_fixed_literals():
    # The gate drops its output tables with fully literal statements: no value
    # is interpolated or concatenated into the SQL. This pins that the set
    # targets exactly the three gate outputs and that each statement is a plain
    # drop-if-exists with no placeholder or format field left in it.
    targets = {"lga_version_drift", "lga_coverage_burden", "stg_version_lineage"}
    seen = set()
    for stmt in d.GATE_DROP_STATEMENTS:
        assert stmt.startswith("drop table if exists ")
        assert "{" not in stmt and "%" not in stmt
        name = stmt.rsplit(" ", 1)[-1].strip('"')
        seen.add(name)
    assert seen == targets
