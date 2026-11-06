"""Tests for scripts/build_enrichment_sources.py and the committed derived inputs.

The derivation functions are exercised on small synthetic frames (the dedupe of
STH/SCH target-population rows, the conflict guard, the latest-known rule and
the input guards); the committed outputs are checked for the 774-LGA coverage
and grain the loader and dbt tests rely on.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
DERIVED = ROOT / "data" / "derived"
_spec = importlib.util.spec_from_file_location(
    "build_enrichment_sources", ROOT / "scripts" / "build_enrichment_sources.py"
)
bes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bes)


def _espen_rows(rows):
    base = {"iuId": 1, "state": "Abia", "canonical_lga": "Aba North",
            "disease": "Schistosomiasis", "disease_code": "SCH"}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_target_population_rows_collapse_to_one_per_year():
    raw = _espen_rows(
        [{"year": 2024, "endemicity": "Non-endemic", "target_pop": t}
         for t in ("SAC", "Total", "Adults", "PreSAC")]
    )
    out = bes.derive_espen(raw)
    assert len(out) == 1
    assert out.loc[0, "years_reported"] == 1


def test_conflicting_labels_for_one_iu_year_abort():
    raw = _espen_rows([
        {"year": 2024, "endemicity": "Non-endemic"},
        {"year": 2024, "endemicity": "High prevalence (50% and above)"},
    ])
    with pytest.raises(SystemExit, match="conflicting"):
        bes.derive_espen(raw)


def test_latest_known_skips_a_recent_unknown():
    raw = _espen_rows([
        {"year": 2020, "endemicity": "Moderate prevalence (10%-49%)"},
        {"year": 2025, "endemicity": "Unknown"},
    ])
    row = bes.derive_espen(raw).iloc[0]
    assert row["endemicity_latest"] == "Unknown"
    assert row["year_latest"] == 2025
    assert row["endemicity_latest_known"] == "Moderate prevalence (10%-49%)"
    assert row["year_latest_known"] == 2020


def test_never_known_stays_null_not_zero():
    raw = _espen_rows([
        {"year": 2024, "endemicity": "Unknown (consider Oncho Elimination Mapping)"},
        {"year": 2025, "endemicity": "Unknown"},
    ])
    row = bes.derive_espen(raw).iloc[0]
    assert pd.isna(row["endemicity_latest_known"])
    assert pd.isna(row["year_latest_known"])


def test_unexpected_disease_is_rejected():
    raw = _espen_rows([{"year": 2024, "endemicity": "Endemic", "disease": "Trachoma"}])
    with pytest.raises(SystemExit, match="unexpected diseases"):
        bes.derive_espen(raw)


def test_is_unknown():
    assert bes.is_unknown("Unknown (under LF MDA)")
    assert bes.is_unknown(None)
    assert not bes.is_unknown("Non-endemic")


def _flood(rows):
    return pd.DataFrame(rows, columns=bes.FLOOD_COLUMNS)


def test_flood_rejects_non_binary_affected():
    with pytest.raises(SystemExit, match="0/1"):
        bes.derive_flood_affected(_flood([["Abia", "Aba North", 2024, 2, "LGA"]]))


def test_flood_rejects_mixed_resolution_within_an_event():
    rows = [["Abia", "Aba North", 2024, 0, "LGA"], ["Abia", "Aba South", 2024, 1, "state"]]
    with pytest.raises(SystemExit, match="mixes"):
        bes.derive_flood_affected(_flood(rows))


def test_flood_rejects_duplicate_lga_event():
    rows = [["Abia", "Aba North", 2024, 0, "LGA"], ["Abia", "Aba North", 2024, 0, "LGA"]]
    with pytest.raises(SystemExit, match="duplicate"):
        bes.derive_flood_affected(_flood(rows))


def test_births_rejects_zero_and_partial_frames():
    frame = pd.DataFrame({"shapeID": ["a", "b"]})
    with pytest.raises(SystemExit, match="positive"):
        bes.derive_births(
            pd.DataFrame({"shapeID": ["a", "b"], "shapeName": ["A", "B"], "births": [1.0, 0.0]}),
            frame,
        )
    with pytest.raises(SystemExit, match="do not match"):
        bes.derive_births(
            pd.DataFrame({"shapeID": ["a"], "shapeName": ["A"], "births": [1.0]}), frame
        )


def test_build_refuses_to_write_into_a_source_directory(tmp_path):
    src = tmp_path / "espen_long.csv"
    src.write_text("x\n")
    with pytest.raises(SystemExit, match="source directory"):
        bes.build(src, src, src, tmp_path)
    assert src.read_text() == "x\n"


# The committed outputs: what the loader reads and the dbt tests then check.

def test_committed_espen_is_774_lgas_by_five_diseases():
    df = pd.read_csv(DERIVED / "ntd_espen_lga.csv")
    assert list(df.columns) == bes.ESPEN_COLUMNS
    assert set(df["disease"]) == bes.ESPEN_DISEASES
    assert not df.duplicated(["state", "canonical_lga", "disease"]).any()
    assert len(df) == 774 * 5


def test_committed_flood_and_births_cover_774():
    flood = pd.read_csv(DERIVED / "flood_event_affected_lga.csv")
    assert flood.groupby("event_year").size().eq(774).all()
    births = pd.read_csv(DERIVED / "births_lga.csv")
    assert births["shapeID"].nunique() == 774
    assert (births["births"] > 0).all()
