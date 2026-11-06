"""Unit tests for the sample-fixture state subsetting.

The fixture is only faithful if the state filter keeps exactly the right rows
and trims whitespace the same way the loader and marts do, so a stray space in
the source does not drop a facility. These pin that behaviour without needing
the full geopackages.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

build_sample_fixture = pytest.importorskip("build_sample_fixture")
subset_states = build_sample_fixture.subset_states


def _df():
    return pd.DataFrame(
        {
            "state": ["Bayelsa", "Ekiti", "Lagos", " Bayelsa ", "Kano"],
            "facility_name": ["a", "b", "c", "d", "e"],
        }
    )


def test_keeps_only_named_states():
    out = subset_states(_df(), "state", ["Bayelsa", "Ekiti"])
    assert set(out["facility_name"]) == {"a", "b", "d"}


def test_trims_whitespace_before_matching():
    # " Bayelsa " must be kept: the loader and marts key on the trimmed name.
    out = subset_states(_df(), "state", ["Bayelsa"])
    assert "d" in set(out["facility_name"])


def test_single_state_selection():
    out = subset_states(_df(), "state", ["Lagos"])
    assert list(out["facility_name"]) == ["c"]


def test_no_match_returns_empty():
    out = subset_states(_df(), "state", ["Sokoto"])
    assert len(out) == 0


def test_does_not_mutate_input():
    df = _df()
    before = len(df)
    subset_states(df, "state", ["Lagos"])
    assert len(df) == before
