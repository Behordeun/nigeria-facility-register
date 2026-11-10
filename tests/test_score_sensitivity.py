"""Tests for the priority-score sensitivity check (scripts/score_sensitivity.py)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("score_sensitivity", ROOT / "scripts" / "score_sensitivity.py")
ss = importlib.util.module_from_spec(_spec)
# Dataclasses look their module up in sys.modules while the class is built.
sys.modules[_spec.name] = ss
_spec.loader.exec_module(ss)


def _lga(label: str, burden: int, coverage: int, access: int, malaria: int) -> "ss.ScoredLga":
    return ss.ScoredLga(label, (burden, coverage, access, malaria), burden + coverage + access + malaria)


LGAS = [
    _lga("a", 3, 3, 2, 3),  # 11, high
    _lga("b", 3, 3, 0, 2),  # 8, high; leans on malaria
    _lga("c", 3, 3, 2, 0),  # 8, high; no malaria
    _lga("d", 2, 2, 0, 3),  # 7
    _lga("e", 0, 0, 0, 0),  # 0
]


def _by_name(results):
    return {r.name: r for r in results}


def test_the_published_weighting_reproduces_the_high_tier_exactly():
    equal = _by_name(ss.compare(LGAS))["equal (published)"]
    assert (equal.group_size, equal.retained, equal.high_size) == (3, 3, 3), "equal weights are the published score"
    assert equal.overlap == pytest.approx(1.0), "the published weighting must overlap the high tier fully"


def test_dropping_a_part_reports_the_lgas_that_leave_the_top_group():
    without_malaria = _by_name(ss.compare(LGAS))["without malaria"]
    # Without malaria: a=8, c=8, b=6, d=4, so the top three are a, c and b.
    assert without_malaria.retained == 3, "a, b and c still lead once malaria is dropped"
    without_access = _by_name(ss.compare(LGAS))["without access"]
    # Without access: a=9, b=8, d=7, c=6, so c leaves and d enters.
    assert (without_access.group_size, without_access.retained) == (3, 2), "c drops out and d comes in"
    assert without_access.overlap == pytest.approx(2 / 4), "two shared over four in either group"


def test_ties_at_the_cut_are_all_kept():
    tied = [_lga("x", 1, 0, 0, 0), _lga("y", 1, 0, 0, 0), _lga("z", 0, 0, 0, 0)]
    assert ss.top_group(tied, dict.fromkeys(ss.PARTS, 1.0), 1) == {"x", "y"}, (
        "a tie at the cut must not be broken by row order"
    )


def test_a_group_swollen_by_ties_lowers_the_overlap():
    swollen = ss.Comparison("t", group_size=6, retained=3, high_size=3)
    assert swollen.overlap == pytest.approx(0.5), "three shared over six combined"


def test_rescaling_gives_every_part_the_same_full_range():
    weights = ss.weightings()["each part rescaled to 0-1"]
    for part, maximum in ss.PART_MAXIMA.items():
        assert weights[part] * maximum == pytest.approx(1.0), f"{part} must span 0-1 after rescaling"


def test_no_high_tier_gives_an_empty_group():
    low = [_lga("e", 0, 0, 0, 0)]
    for result in ss.compare(low):
        assert (result.group_size, result.retained) == (0, 0), f"{result.name} has no high tier to compare"


def test_load_reads_only_scored_lgas(tmp_path):
    db = tmp_path / "register.duckdb"
    with duckdb.connect(str(db)) as conn:
        conn.execute("""
            create table lga_coverage_burden as select * from (values
                ('a', 3, 3, 2, 3, 11),
                ('n', null, 3, 2, 3, null)
            ) t(lga_label, priority_burden_points, priority_coverage_points,
                access_risk_score, priority_malaria_points, priority_score)
        """)
    assert ss.load(db) == [ss.ScoredLga("a", (3, 3, 2, 3), 11)], "unscored LGAs must be left out"


def test_load_names_the_build_when_the_register_is_missing(tmp_path):
    with pytest.raises(SystemExit, match="make build"):
        ss.load(tmp_path / "absent.duckdb")


def test_report_prints_one_line_per_weighting():
    lines = ss.report(LGAS).splitlines()
    assert lines[0] == "5 scored LGAs, 3 in the high tier (score 8+)"
    assert len(lines) == 3 + len(ss.weightings()), "header, blank, column titles, then one row per weighting"
