"""Unit tests for the refresh's input fingerprint.

The sensor rebuilds only when the inputs actually change, and the live register
records the fingerprint it was built from, so the fingerprint must be stable for
unchanged inputs and change whenever anything that shapes the register changes:
a geopackage, a frame or derived table, a dbt model, or the population year.
"""
from __future__ import annotations

import os
from datetime import date

import pytest

from nfr.orchestration import inputs, snapshot

GPKGS = ["a.gpkg", "b.gpkg"]


def _tree(root):
    """A minimal input set: raw geopackages, frame, derived, and a dbt project."""
    raw, frame, derived, dbt = (root / n for n in ("raw", "frame", "derived", "dbt"))
    for d in (raw, frame, derived, dbt / "models", dbt / "tests"):
        d.mkdir(parents=True)
    (raw / "a.gpkg").write_bytes(b"A" * 100)
    (raw / "b.gpkg").write_bytes(b"B" * 100)
    (frame / "index.csv").write_text("lga\nX\n")
    (derived / inputs.POPULATION_TABLE).write_text("shapeID,population_year\ns1,2025\ns1,2026\ns1,2030\n")
    (dbt / "dbt_project.yml").write_text("name: nfr\n")
    (dbt / "models" / "mart.sql").write_text("select 1\n")
    (dbt / "tests" / "check.sql").write_text("select 1 where false\n")
    return raw, frame, derived, dbt


def _fp(root, year=2026):
    raw, frame, derived, dbt = (root / n for n in ("raw", "frame", "derived", "dbt"))
    return inputs.fingerprint(raw, GPKGS, frame, derived, inputs.dbt_project_digest(dbt), year)


def test_stable_for_unchanged_inputs(tmp_path):
    _tree(tmp_path)
    first = _fp(tmp_path)
    second = _fp(tmp_path)
    assert first == second, "unchanged inputs must fingerprint the same"


@pytest.mark.parametrize("path", ["raw/a.gpkg", "frame/index.csv", f"derived/{inputs.POPULATION_TABLE}"])
def test_changes_when_any_input_file_changes(tmp_path, path):
    _tree(tmp_path)
    before = _fp(tmp_path)
    target = tmp_path / path
    target.write_bytes(target.read_bytes() + b"\n")
    assert _fp(tmp_path) != before, f"editing {path} must change the fingerprint"


def test_changes_when_a_derived_table_is_added(tmp_path):
    _tree(tmp_path)
    before = _fp(tmp_path)
    (tmp_path / "derived" / "malaria_lga.csv").write_text("shapeID\ns1\n")
    assert _fp(tmp_path) != before, "a new enrichment table must change the fingerprint"


def test_changes_when_a_dbt_model_changes(tmp_path):
    _tree(tmp_path)
    before = _fp(tmp_path)
    (tmp_path / "dbt" / "models" / "mart.sql").write_text("select 2\n")
    assert _fp(tmp_path) != before, "a model edit changes the marts, so it must change the fingerprint"


def test_a_dbt_test_edit_does_not_change_the_fingerprint(tmp_path):
    # dbt tests gate a build but never change its output, so editing one must
    # not republish an identical register.
    _tree(tmp_path)
    before = _fp(tmp_path)
    (tmp_path / "dbt" / "tests" / "check.sql").write_text("select 1 where 1 = 0\n")
    assert _fp(tmp_path) == before


def test_changes_with_the_population_year(tmp_path):
    _tree(tmp_path)
    assert _fp(tmp_path, 2026) != _fp(tmp_path, 2027), "a new population year must change the fingerprint"


def test_same_file_name_in_two_folders_is_kept_distinct(tmp_path):
    raw, frame, derived, dbt = _tree(tmp_path)
    (frame / "shared.csv").write_text("x\n")
    in_frame = _fp(tmp_path)
    (frame / "shared.csv").unlink()
    (derived / "shared.csv").write_text("x\n")
    assert _fp(tmp_path) != in_frame


def test_detects_same_size_same_mtime_content_change(tmp_path):
    _tree(tmp_path)
    f = tmp_path / "raw" / "a.gpkg"
    st = f.stat()
    before = _fp(tmp_path)
    f.write_bytes(b"Z" * 100)
    os.utime(f, (st.st_atime, st.st_mtime))
    assert _fp(tmp_path) != before


def test_rejects_a_symlink_escaping_its_folder(tmp_path):
    _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_text("x\n")
    (tmp_path / "derived" / "leak.csv").symlink_to(outside / "secret.csv")
    with pytest.raises(ValueError, match="escapes the watched directory"):
        _fp(tmp_path)


def test_a_snapshot_fingerprints_the_same_as_the_inputs_it_copies(tmp_path):
    # The run verifies its snapshot against the sensor's fingerprint, so a
    # faithful snapshot of every input folder must fingerprint identically.
    raw, frame, derived, dbt = _tree(tmp_path)
    snap = tmp_path / "snap"
    snapshot.create_snapshot(raw, snap, GPKGS)
    snapshot.create_snapshot(frame, snap / inputs.FRAME_SUBDIR, inputs.input_files(frame))
    snapshot.create_snapshot(derived, snap / inputs.DERIVED_SUBDIR, inputs.input_files(derived))
    digest = inputs.dbt_project_digest(dbt)
    live = inputs.fingerprint(raw, GPKGS, frame, derived, digest, 2026)
    copied = inputs.fingerprint(snap, GPKGS, snap / inputs.FRAME_SUBDIR, snap / inputs.DERIVED_SUBDIR, digest, 2026)
    assert copied == live


@pytest.mark.parametrize(("today", "expected"), [
    (date(2024, 6, 1), 2025),  # before the table starts: its first year
    (date(2026, 1, 1), 2026),  # inside the table: the calendar year
    (date(2031, 3, 1), 2030),  # after it ends: its last year
])
def test_population_year_is_the_calendar_year_capped_to_the_table(tmp_path, today, expected):
    _tree(tmp_path)
    table = tmp_path / "derived" / inputs.POPULATION_TABLE
    assert inputs.select_population_year(table, today) == expected


def test_population_year_rejects_an_empty_table(tmp_path):
    table = tmp_path / inputs.POPULATION_TABLE
    table.write_text("shapeID,population_year\n")
    today = date(2026, 1, 1)
    with pytest.raises(ValueError, match="carries no population_year"):
        inputs.select_population_year(table, today)


def test_population_year_rejects_a_table_without_the_year_column(tmp_path):
    table = tmp_path / inputs.POPULATION_TABLE
    table.write_text("shapeID,population\ns1,10\n")
    today = date(2026, 1, 1)
    with pytest.raises(ValueError, match="has no population_year column"):
        inputs.select_population_year(table, today)
