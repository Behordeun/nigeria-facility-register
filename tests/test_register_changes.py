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
from dagster import build_asset_context

from nfr.orchestration import assets, changes, config, gates

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


def test_a_failed_change_record_stops_the_publish_before_promote(tmp_path, monkeypatch):
    live = tmp_path / "nfr.duckdb"
    live.write_bytes(b"the live register")
    staging = tmp_path / "staging.duckdb"
    staging.write_bytes(b"the staged build")
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    (snapshot_dir / config.SNAPSHOT_YEAR_FILE).write_text("2026")
    promoted = []

    def fail(*_args):
        raise duckdb.IOException("disk full")

    monkeypatch.setattr(config, "DB", live)
    monkeypatch.setattr(assets, "_staged_inputs", lambda _context: (staging, snapshot_dir))
    monkeypatch.setattr(config, "snapshot_fingerprint", lambda *_args: "new-fp")
    monkeypatch.setattr(gates, "enforce_carryover_gate", lambda _db: None)
    monkeypatch.setattr(assets.metrics, "collect_from_path", lambda _db: None)
    monkeypatch.setattr(changes, "write_register_changes", fail)
    monkeypatch.setattr(gates, "promote", lambda *args: promoted.append(args))
    with pytest.raises(duckdb.IOException):
        assets.published_register(build_asset_context())
    assert promoted == [], "nothing is promoted when the change record fails"
    assert not staging.exists(), "the staged build is discarded"
    assert live.read_bytes() == b"the live register", "the live register is untouched"
