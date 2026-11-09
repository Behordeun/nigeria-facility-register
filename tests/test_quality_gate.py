"""Unit tests for the data-quality gate and the atomic-publish staging path.

The gate's blocking carry-over floor must sit below the advisory warn floor, or
the blocking check would fire before (or together with) the warning and the two
severities would collapse into one. dagster is an optional install, so skip when
it is absent (the gates module imports dagster at the top).
"""
import pytest

pytest.importorskip("dagster")

from nfr.orchestration import config, gates  # noqa: E402


def test_hard_carryover_floor_is_below_the_warn_floor():
    # The gate (blocking) must trip only on a genuine collapse, strictly below
    # the advisory warn floor, so a dip warns before it ever blocks.
    assert gates.HARD_MIN_CARRYOVER_RATE < gates.MIN_CARRYOVER_RATE


def test_floors_are_fractions():
    for floor in (gates.MIN_LGA_RESOLVE_RATE, gates.MIN_CARRYOVER_RATE, gates.HARD_MIN_CARRYOVER_RATE):
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
    assert gates.enforce_carryover_gate(db) == pytest.approx(0.9)


def test_gate_raises_when_rate_below_floor(tmp_path):
    # 1 carried of 10 expected = 0.1, below the 0.30 hard floor.
    db = _gate_db(tmp_path, v2_in_covered=10, carried=1)
    with pytest.raises(RuntimeError, match="data-quality gate failed"):
        gates.enforce_carryover_gate(db)


def test_gate_raises_when_rate_is_undefined(tmp_path):
    # No v2 facilities in a covered state: rate is None, which must fail the
    # gate rather than pass as "nothing to check".
    db = _gate_db(tmp_path, v2_in_covered=0, carried=0)
    with pytest.raises(RuntimeError, match="data-quality gate failed"):
        gates.enforce_carryover_gate(db)


def test_staging_db_path_has_a_single_dot_and_is_run_scoped():
    # dbt-duckdb reads a path like nfr.<id>.duckdb as a catalog.schema
    # reference, so the stem must carry no interior dot. Two different run ids
    # must map to two different side files, and both must sit under STAGING_DIR.
    a = config.staging_db_path("33e0370b-89fc-4e29")
    b = config.staging_db_path("other-run-id")
    assert a.name == "nfr_33e0370b-89fc-4e29.duckdb"
    assert a.name.count(".") == 1
    assert a != b
    assert a.parent == config.STAGING_DIR


def test_staging_db_path_sanitises_and_falls_back():
    # A run id with path-unsafe characters is stripped to safe ones; an empty
    # result falls back to a fixed name rather than producing a bare "nfr_.duckdb"
    # that could collide or escape the staging dir.
    assert config.staging_db_path("../../etc/passwd").name.startswith("nfr_")
    assert "/" not in config.staging_db_path("../../etc/passwd").name
    assert config.staging_db_path("").name == "nfr_manual.duckdb"


def test_failed_publish_discards_the_side_db_and_keeps_the_live_db(tmp_path, monkeypatch):
    live = tmp_path / "nfr.duckdb"
    live.write_bytes(b"previous register")
    staging = tmp_path / "staging" / "nfr_run.duckdb"
    staging.parent.mkdir()
    staging.write_bytes(b"new register")
    snap = tmp_path / "snapshots" / "run"
    snap.mkdir(parents=True)
    (snap / "a.gpkg").write_bytes(b"A")
    monkeypatch.setattr(config, "DB", live)
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "published")
    monkeypatch.setattr(config, "HOLD_FILE", tmp_path / "hold.json")

    def fail(*_args):
        raise RuntimeError("sentinel write failed")

    monkeypatch.setattr(gates.publish, "write_published_fingerprint", fail)
    with pytest.raises(RuntimeError, match="sentinel write failed"):
        gates.promote(staging, "fp", snap)
    assert live.read_bytes() == b"previous register", "a failed publish must leave the live register untouched"
    assert not staging.exists(), "a failed publish must discard the side database"
    assert not snap.exists(), "a failed publish must clear the run snapshot"


def test_promote_replaces_the_live_db_with_the_side_db(tmp_path, monkeypatch):
    live = tmp_path / "nfr.duckdb"
    live.write_bytes(b"previous register")
    staging = tmp_path / "nfr_run.duckdb"
    staging.write_bytes(b"new register")
    monkeypatch.setattr(config, "DB", live)
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "published")
    monkeypatch.setattr(config, "HOLD_FILE", tmp_path / "hold.json")
    monkeypatch.setattr(gates.publish, "write_published_fingerprint", lambda *_args: None)
    gates.promote(staging, "fp", tmp_path / "snapshots" / "run")
    assert live.read_bytes() == b"new register", "promote must move the side database onto the live path"
    assert not staging.exists(), "the side database is renamed, not copied"


def test_promote_archives_the_outgoing_register_and_lifts_a_hold(tmp_path, monkeypatch):
    from nfr.orchestration import registers

    live = tmp_path / "nfr.duckdb"
    live.write_bytes(b"previous register")
    staging = tmp_path / "nfr_run.duckdb"
    staging.write_bytes(b"new register")
    archive = tmp_path / "published"
    hold = tmp_path / "hold.json"
    registers.write_hold(hold, "rolled-back", "test")
    monkeypatch.setattr(config, "DB", live)
    monkeypatch.setattr(config, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(config, "HOLD_FILE", hold)
    monkeypatch.setattr(gates.publish, "write_published_fingerprint", lambda *_args: None)
    gates.promote(staging, "fp", tmp_path / "snapshots" / "run")
    kept = registers.archived(archive)
    assert [p.read_bytes() for p in kept] == [b"previous register"], "the outgoing register must be archived"
    assert live.read_bytes() == b"new register"
    assert registers.read_hold(hold) is None, "publishing a different register lifts a rollback hold"


def test_promote_keeps_only_the_newest_archived_registers(tmp_path, monkeypatch):
    from nfr.orchestration import registers

    live = tmp_path / "nfr.duckdb"
    live.write_bytes(b"r0")
    archive = tmp_path / "published"
    monkeypatch.setattr(config, "DB", live)
    monkeypatch.setattr(config, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(config, "HOLD_FILE", tmp_path / "hold.json")
    monkeypatch.setattr(gates.publish, "write_published_fingerprint", lambda *_args: None)
    for i in range(1, 6):
        staging = tmp_path / f"nfr_run{i}.duckdb"
        staging.write_bytes(f"r{i}".encode())
        gates.promote(staging, f"fp{i}", tmp_path / "snapshots" / "run")
    kept = registers.archived(archive)
    assert len(kept) == config.ARCHIVE_KEEP, f"expected the newest {config.ARCHIVE_KEEP}, kept {len(kept)}"
    assert [p.read_bytes() for p in kept] == [b"r2", b"r3", b"r4"], "the oldest registers are pruned first"


def test_publishing_the_held_vintage_keeps_the_hold(tmp_path, monkeypatch):
    # The refresh never builds a held vintage, but should any other path publish
    # it, the hold must survive so the rollback protection is not silently lost.
    from nfr.orchestration import registers

    live = tmp_path / "nfr.duckdb"
    live.write_bytes(b"restored register")
    staging = tmp_path / "nfr_run.duckdb"
    staging.write_bytes(b"held vintage")
    hold = tmp_path / "hold.json"
    registers.write_hold(hold, "held-fp", "test")
    monkeypatch.setattr(config, "DB", live)
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "published")
    monkeypatch.setattr(config, "HOLD_FILE", hold)
    monkeypatch.setattr(gates.publish, "write_published_fingerprint", lambda *_args: None)
    gates.promote(staging, "held-fp", tmp_path / "snapshots" / "run")
    assert registers.read_hold(hold) == "held-fp", "publishing the held vintage must not lift its hold"
