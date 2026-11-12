"""What changed between the outgoing register and the one being published.

The record must list tier moves, match changes and the largest facility moves
against the register it replaces, write a summary only when there is nothing to
compare with, never touch the live register, and stop the publish when it
cannot be written.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from nfr.orchestration import changes, config, gates, publish, registers

_FULL = ("state varchar, canonical_lga varchar, lga_label varchar, priority_tier varchar, "
         "facilities bigint, facility_status varchar")


def _register(path: Path, rows: list[tuple], columns: str = _FULL) -> Path:
    con = duckdb.connect(str(path))
    con.execute(f"create table lga_coverage_burden ({columns})")
    if rows:
        con.executemany(f"insert into lga_coverage_burden values ({', '.join('?' * len(rows[0]))})", rows)
    con.close()
    return path


def _lga(name: str, tier: str = "low", facilities: int = 10, status: str = "current", state: str = "Kano"):
    return (state, name, f"{name}, {state}", tier, facilities, status)


def _changes(staging: Path) -> list[dict]:
    con = duckdb.connect(str(staging), read_only=True)
    try:
        cursor = con.execute("select * from register_changes order by change_order")
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    finally:
        con.close()


def test_a_first_publish_records_only_the_summary(tmp_path):
    staging = _register(tmp_path / "staging.duckdb", [_lga("Ajingi")])
    changes.write_register_changes(staging, tmp_path / "absent.duckdb", "new-fp", None)
    rows = _changes(staging)
    assert len(rows) == 1, "with no previous register only the summary is written"
    assert rows[0]["change"] == "summary"
    assert (rows[0]["previous"], rows[0]["current"]) == (None, "new-fp")
    assert "first publish" in rows[0]["detail"]


def test_tier_match_and_facility_changes_are_listed_in_order(tmp_path):
    live = _register(tmp_path / "live.duckdb", [
        _lga("Ajingi", tier="medium", facilities=10),
        _lga("Bebeji", tier="low", facilities=0, status="unmatched"),
        _lga("Bichi", tier="high", facilities=20),
        _lga("Dala", tier="low", facilities=30),
        _lga("Gaya", tier="low", facilities=5),
    ])
    staging = _register(tmp_path / "staging.duckdb", [
        _lga("Ajingi", tier="high", facilities=10),
        _lga("Bebeji", tier="low", facilities=3),
        _lga("Bichi", tier="medium", facilities=20),
        _lga("Dala", tier="low", facilities=22),
        _lga("Gaya", tier="low", facilities=5),
    ])
    changes.write_register_changes(staging, live, "new-fp", "old-fp")
    rows = _changes(staging)
    summary = rows[0]
    assert (summary["change"], summary["previous"], summary["current"]) == ("summary", "old-fp", "new-fp")
    assert summary["detail"].startswith("2 LGA(s) changed priority tier; 1 changed whether any facility matches; "
                                        "2 changed facility count")
    listed = [(r["change"], r["canonical_lga"], r["previous"], r["current"], r["facilities_change"])
              for r in rows[1:]]
    assert listed == [
        ("priority tier", "Ajingi", "medium", "high", None),
        ("priority tier", "Bichi", "high", "medium", None),
        ("facility match", "Bebeji", "no facility", "facilities matched", 3),
        ("facilities", "Dala", "30", "22", -8),
        ("facilities", "Bebeji", "0", "3", 3),
    ], "tier moves (to high first), then match changes, then facility moves by size"
    assert [r["change_order"] for r in rows] == list(range(len(rows)))


def test_only_the_largest_facility_moves_are_listed(tmp_path):
    count = changes.FACILITY_CHANGES_LISTED + 5
    live = _register(tmp_path / "live.duckdb", [_lga(f"L{i:02d}", facilities=100) for i in range(count)])
    staging = _register(tmp_path / "staging.duckdb",
                        [_lga(f"L{i:02d}", facilities=100 + i + 1) for i in range(count)])
    changes.write_register_changes(staging, live, "new-fp", "old-fp")
    moves = [r for r in _changes(staging) if r["change"] == "facilities"]
    assert len(moves) == changes.FACILITY_CHANGES_LISTED
    assert moves[0]["facilities_change"] == count, "the largest move comes first"
    assert f"{count} changed facility count ({changes.FACILITY_CHANGES_LISTED} largest listed)" in (
        _changes(staging)[0]["detail"])


def test_an_older_register_without_the_compared_columns_gets_a_summary_only(tmp_path):
    live = _register(tmp_path / "live.duckdb", [("Kano", "Ajingi", 10)],
                     columns="state varchar, canonical_lga varchar, facilities bigint")
    staging = _register(tmp_path / "staging.duckdb", [_lga("Ajingi")])
    changes.write_register_changes(staging, live, "new-fp", "old-fp")
    rows = _changes(staging)
    assert [r["change"] for r in rows] == ["summary"]
    assert "predates" in rows[0]["detail"]


def test_the_live_register_is_only_read(tmp_path):
    live = _register(tmp_path / "live.duckdb", [_lga("Ajingi", tier="medium")])
    before = live.read_bytes()
    staging = _register(tmp_path / "staging.duckdb", [_lga("Ajingi", tier="high")])
    changes.write_register_changes(staging, live, "new-fp", "old-fp")
    assert live.read_bytes() == before, "the comparison must never write to the live register"


def test_the_table_has_the_published_columns(tmp_path):
    staging = _register(tmp_path / "staging.duckdb", [_lga("Ajingi")])
    changes.write_register_changes(staging, tmp_path / "absent.duckdb", "new-fp", None)
    con = duckdb.connect(str(staging), read_only=True)
    columns = dict(con.execute("select column_name, data_type from information_schema.columns "
                               "where table_name = 'register_changes' order by ordinal_position").fetchall())
    con.close()
    assert columns == changes.COLUMNS


def test_a_failed_change_record_discards_the_staged_build(tmp_path, monkeypatch):
    # A real live register, so the failure comes from writing the record, not
    # from reading the live register's fingerprint.
    live = _register(tmp_path / "nfr.duckdb", [_lga("Ajingi")])
    before = live.read_bytes()
    staging = _register(tmp_path / "staging.duckdb", [_lga("Ajingi")])
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()

    def fail(*_args):
        raise duckdb.IOException("disk full")

    monkeypatch.setattr(config, "DB", live)
    monkeypatch.setattr(gates, "enforce_carryover_gate", lambda _db: None)
    monkeypatch.setattr(gates.metrics, "collect_from_path", lambda _db: None)
    monkeypatch.setattr(changes, "write_register_changes", fail)
    with pytest.raises(duckdb.IOException, match="disk full"):
        gates.check_staged_build(staging, "new-fp", snapshot_dir)
    assert not staging.exists(), "the staged build is discarded, so nothing can be promoted"
    assert not snapshot_dir.exists(), "the run's snapshot is cleared"
    assert live.read_bytes() == before, "the live register is untouched"


def test_a_checked_build_carries_its_change_record(tmp_path, monkeypatch):
    live = _register(tmp_path / "nfr.duckdb", [_lga("Ajingi", tier="medium")])
    publish.write_published_fingerprint(str(live), "live-fp")
    staging = _register(tmp_path / "staging.duckdb", [_lga("Ajingi", tier="high")])
    monkeypatch.setattr(config, "DB", live)
    monkeypatch.setattr(gates, "enforce_carryover_gate", lambda _db: None)
    monkeypatch.setattr(gates.metrics, "collect_from_path", lambda _db: "metrics")
    assert gates.check_staged_build(staging, "new-fp", tmp_path / "snapshot") == "metrics"
    rows = _changes(staging)
    assert [r["change"] for r in rows] == ["summary", "priority tier"]
    assert (rows[0]["previous"], rows[0]["current"]) == ("live-fp", "new-fp"), (
        "the summary names the live register's fingerprint and the build's own"
    )


def test_lgas_in_only_one_register_are_counted_not_compared(tmp_path):
    live = _register(tmp_path / "live.duckdb", [_lga("Ajingi"), _lga("Old name")])
    staging = _register(tmp_path / "staging.duckdb", [_lga("Ajingi"), _lga("New name"), _lga("Split off")])
    changes.write_register_changes(staging, live, "new-fp", "old-fp")
    rows = _changes(staging)
    assert [r["change"] for r in rows] == ["summary"], "an LGA without a counterpart has nothing to compare"
    assert "2 LGA(s) are only in this register and 1 only in the previous one" in rows[0]["detail"]


def test_ensure_record_adds_a_summary_only_once(tmp_path):
    db = _register(tmp_path / "old.duckdb", [_lga("Ajingi")])
    publish.write_published_fingerprint(str(db), "old-fp")
    assert changes.ensure_record(db, "restored") is True
    assert changes.ensure_record(db, "again") is False, "an existing record is kept"
    rows = _changes(db)
    assert [(r["change"], r["previous"], r["current"], r["detail"]) for r in rows] == [
        ("summary", None, "old-fp", "restored")]


def test_restoring_an_archive_from_before_change_records_gives_it_one(tmp_path):
    archive = tmp_path / "published"
    live = _register(tmp_path / "nfr.duckdb", [_lga("Ajingi")])
    publish.write_published_fingerprint(str(live), "old-fp")
    kept = registers.archive_live(live, archive)
    archived_bytes = kept.read_bytes()
    live.unlink()
    staging = _register(tmp_path / "staging.duckdb", [_lga("Ajingi")])
    changes.write_register_changes(staging, tmp_path / "absent.duckdb", "new-fp", None)
    staging.rename(live)

    registers.restore(kept, live, archive, tmp_path / "hold.json", keep=3)

    rows = _changes(live)
    assert [r["change"] for r in rows] == ["summary"]
    assert rows[0]["current"] == "old-fp"
    assert "restored from an archive made before change records" in rows[0]["detail"]
    assert kept.read_bytes() == archived_bytes, "the archive itself is left as it was"
