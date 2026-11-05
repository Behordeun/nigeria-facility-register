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
