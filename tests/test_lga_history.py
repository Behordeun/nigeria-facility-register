"""Per-LGA history across published registers.

The history must gain one vintage per new input fingerprint and none for a
republish of the same inputs, carry earlier vintages forward from the live
register (or seed them from an older register's own figures), survive a
rollback consistently, never write to the live register, and stop the publish
when it cannot be written.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from nfr.orchestration import changes, config, gates, history, publish, registers

LGAS = [("Kano", "Ajingi"), ("Kano", "Bebeji")]


def _build(path: Path, fingerprint: str | None, built_at: str, facilities: int, with_history=None) -> Path:
    """A register as a refresh leaves it: the coverage mart, the summary and the sentinel."""
    con = duckdb.connect(str(path))
    con.execute("create table lga_coverage_burden (state varchar, canonical_lga varchar, lga_label varchar, "
                "facilities bigint, facilities_per_10k_population double, priority_score integer, "
                "priority_tier varchar)")
    con.executemany("insert into lga_coverage_burden values (?, ?, ?, ?, ?, ?, ?)",
                    [(s, lga, f"{lga}, {s}", facilities, facilities / 10, 5, "medium") for s, lga in LGAS])
    con.execute("create table register_summary (built_at timestamp)")
    con.execute("insert into register_summary values (?::timestamp)", [built_at])
    con.close()
    if fingerprint is not None:
        publish.write_published_fingerprint(str(path), fingerprint)
    if with_history is not None:
        history.write_lga_history(path, with_history, fingerprint)
    return path


def _history(db: Path) -> list[tuple]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute("select fingerprint, canonical_lga, facilities from lga_history "
                           "order by built_at, canonical_lga").fetchall()
    finally:
        con.close()


def _publish(tmp_path: Path, live: Path, fingerprint: str, built_at: str, facilities: int) -> Path:
    """Build a staged register, write its history against the live one, and promote it by rename."""
    staging = _build(tmp_path / f"staging-{fingerprint}.duckdb", None, built_at, facilities)
    history.write_lga_history(staging, live, fingerprint)
    publish.write_published_fingerprint(str(staging), fingerprint)
    staging.replace(live)
    return live


def test_a_first_publish_starts_the_history_with_its_own_vintage(tmp_path):
    staging = _build(tmp_path / "staging.duckdb", None, "2026-11-01 06:00", 10)
    history.write_lga_history(staging, tmp_path / "absent.duckdb", "fp1")
    assert _history(staging) == [("fp1", "Ajingi", 10), ("fp1", "Bebeji", 10)]


def test_three_publishes_give_three_vintages_per_lga(tmp_path):
    live = tmp_path / "nfr.duckdb"
    for i, fp in enumerate(["fp1", "fp2", "fp3"], start=1):
        _publish(tmp_path, live, fp, f"2026-11-0{i} 06:00", 10 * i)
    rows = _history(live)
    assert [r for r in rows if r[1] == "Ajingi"] == [("fp1", "Ajingi", 10), ("fp2", "Ajingi", 20),
                                                     ("fp3", "Ajingi", 30)]
    assert len(rows) == 3 * len(LGAS), "one row per (vintage, LGA)"


def test_republishing_the_same_inputs_adds_no_rows(tmp_path):
    live = tmp_path / "nfr.duckdb"
    _publish(tmp_path, live, "fp1", "2026-11-01 06:00", 10)
    _publish(tmp_path, live, "fp1", "2026-11-08 06:00", 10)
    assert _history(live) == [("fp1", "Ajingi", 10), ("fp1", "Bebeji", 10)], (
        "the weekly refresh of unchanged inputs must not repeat a vintage"
    )


def test_an_older_live_register_contributes_its_own_vintage(tmp_path):
    live = _build(tmp_path / "nfr.duckdb", "fp0", "2026-10-01 06:00", 7)
    staging = _build(tmp_path / "staging.duckdb", None, "2026-11-01 06:00", 9)
    history.write_lga_history(staging, live, "fp1")
    assert _history(staging) == [("fp0", "Ajingi", 7), ("fp0", "Bebeji", 7), ("fp1", "Ajingi", 9),
                                 ("fp1", "Bebeji", 9)]


def test_a_rollback_then_publish_keeps_one_consistent_history(tmp_path):
    live = tmp_path / "nfr.duckdb"
    archive = tmp_path / "published"
    _publish(tmp_path, live, "fp1", "2026-11-01 06:00", 10)
    _publish(tmp_path, live, "fp2", "2026-11-02 06:00", 20)
    kept_fp2 = registers.archive_live(live, archive)
    _publish(tmp_path, live, "fp3", "2026-11-03 06:00", 30)

    registers.restore(kept_fp2, live, archive, tmp_path / "hold.json", keep=5)
    assert [r[0] for r in _history(live)] == ["fp1", "fp1", "fp2", "fp2"], "a rollback brings back its own history"

    _publish(tmp_path, live, "fp4", "2026-11-04 06:00", 40)
    fingerprints = [r[0] for r in _history(live)]
    assert fingerprints == ["fp1", "fp1", "fp2", "fp2", "fp4", "fp4"], (
        "the rolled-back vintage is gone and every remaining vintage appears once"
    )


def test_the_live_register_is_only_read(tmp_path):
    live = _build(tmp_path / "nfr.duckdb", "fp0", "2026-10-01 06:00", 7)
    before = live.read_bytes()
    staging = _build(tmp_path / "staging.duckdb", None, "2026-11-01 06:00", 9)
    history.write_lga_history(staging, live, "fp1")
    assert live.read_bytes() == before


def test_the_table_has_the_published_columns(tmp_path):
    staging = _build(tmp_path / "staging.duckdb", None, "2026-11-01 06:00", 9)
    history.write_lga_history(staging, tmp_path / "absent.duckdb", "fp1")
    con = duckdb.connect(str(staging), read_only=True)
    columns = dict(con.execute("select column_name, data_type from information_schema.columns "
                               "where table_name = 'lga_history' order by ordinal_position").fetchall())
    con.close()
    assert columns == history.COLUMNS


def test_ensure_history_gives_a_register_its_own_vintage_once(tmp_path):
    db = _build(tmp_path / "old.duckdb", "fp0", "2026-10-01 06:00", 7)
    assert history.ensure_history(db) is True
    assert history.ensure_history(db) is False, "an existing history is kept"
    assert _history(db) == [("fp0", "Ajingi", 7), ("fp0", "Bebeji", 7)]


def test_ensure_history_gives_an_undescribable_register_an_empty_table(tmp_path):
    db = tmp_path / "bare.duckdb"
    duckdb.connect(str(db)).close()
    assert history.ensure_history(db) is True
    assert _history(db) == [], "the table exists, so the dashboard finds it, but there is nothing to record"


def test_a_failed_history_discards_the_staged_build(tmp_path, monkeypatch):
    live = _build(tmp_path / "nfr.duckdb", "fp0", "2026-10-01 06:00", 7)
    before = live.read_bytes()
    staging = _build(tmp_path / "staging.duckdb", None, "2026-11-01 06:00", 9)
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()

    def fail(*_args):
        raise duckdb.IOException("disk full")

    monkeypatch.setattr(config, "DB", live)
    monkeypatch.setattr(gates, "enforce_carryover_gate", lambda _db: None)
    monkeypatch.setattr(gates.metrics, "collect_from_path", lambda _db: None)
    monkeypatch.setattr(changes, "write_register_changes", lambda *_args: None)
    monkeypatch.setattr(history, "write_lga_history", fail)
    with pytest.raises(duckdb.IOException, match="disk full"):
        gates.check_staged_build(staging, "fp1", snapshot_dir)
    assert not staging.exists(), "the staged build is discarded, so nothing can be promoted"
    assert live.read_bytes() == before
