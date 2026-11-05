"""Unit tests for the data-quality gate and the atomic-publish staging path.

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
    v2 facilities and `carried` carried-over lineage rows."""
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
    con.close()
    return str(p)


def test_gate_returns_rate_when_it_clears_floor(tmp_path):
    # 9 carried of 10 expected = 0.9, well above the hard floor.
    db = _gate_db(tmp_path, v2_in_covered=10, carried=9)
    assert d._enforce_carryover_gate(db) == pytest.approx(0.9)


def test_gate_raises_when_rate_below_floor(tmp_path):
    # 1 carried of 10 expected = 0.1, below the 0.30 hard floor.
    db = _gate_db(tmp_path, v2_in_covered=10, carried=1)
    with pytest.raises(RuntimeError, match="data-quality gate failed"):
        d._enforce_carryover_gate(db)


def test_gate_raises_when_rate_is_undefined(tmp_path):
    # No v2 facilities in a covered state: rate is None, which must fail the
    # gate rather than pass as "nothing to check".
    db = _gate_db(tmp_path, v2_in_covered=0, carried=0)
    with pytest.raises(RuntimeError, match="data-quality gate failed"):
        d._enforce_carryover_gate(db)


def test_staging_db_path_has_a_single_dot_and_is_run_scoped():
    # dbt-duckdb reads a path like nfr.<id>.duckdb as a catalog.schema
    # reference, so the stem must carry no interior dot. Two different run ids
    # must map to two different side files, and both must sit under STAGING_DIR.
    a = d._staging_db_path("33e0370b-89fc-4e29")
    b = d._staging_db_path("other-run-id")
    assert a.name == "nfr_33e0370b-89fc-4e29.duckdb"
    assert a.name.count(".") == 1
    assert a != b
    assert a.parent == d.STAGING_DIR


def test_staging_db_path_sanitises_and_falls_back():
    # A run id with path-unsafe characters is stripped to safe ones; an empty
    # result falls back to a fixed name rather than producing a bare "nfr_.duckdb"
    # that could collide or escape the staging dir.
    assert d._staging_db_path("../../etc/passwd").name.startswith("nfr_")
    assert "/" not in d._staging_db_path("../../etc/passwd").name
    assert d._staging_db_path("").name == "nfr_manual.duckdb"
