"""
How much the high-priority list depends on the priority score's weights.

The priority score adds four parts with equal weight (docs/adr/0004). This
re-ranks the scored LGAs under alternative weightings and reports, for each,
how many of today's high-tier LGAs would still be in the top group of the same
size, and the overlap of the two groups (shared over combined, so a top group
swollen by ties scores lower). A weighting with a high overlap says the list is
robust to that choice; a low one says the choice matters and must be argued for.

It only reads the register (the database is opened read-only).

Usage:
    python scripts/score_sensitivity.py                 # build/scratch.duckdb
    python scripts/score_sensitivity.py --db build/nfr.duckdb
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path("build") / "scratch.duckdb"
PARTS = ("burden", "coverage", "access", "malaria")
PART_MAXIMA = {"burden": 3, "coverage": 3, "access": 2, "malaria": 3}
HIGH_TIER_MIN_SCORE = 8
_QUERY = """
    select lga_label,
           priority_burden_points, priority_coverage_points,
           access_risk_score, priority_malaria_points,
           priority_score
    from main.lga_coverage_burden
    where priority_score is not null
"""


@dataclass(frozen=True)
class ScoredLga:
    """One LGA's published score parts, in PARTS order, and its total."""

    label: str
    parts: tuple[int, int, int, int]
    score: int


@dataclass(frozen=True)
class Comparison:
    """How one alternative weighting's top group relates to the high tier."""

    name: str
    group_size: int
    retained: int
    high_size: int

    @property
    def overlap(self) -> float:
        """Shared LGAs over all LGAs in either group (1.0 means identical)."""
        union = self.group_size + self.high_size - self.retained
        return self.retained / union if union else 1.0


def weightings() -> dict[str, dict[str, float]]:
    """The alternatives to compare: each part dropped, each part doubled, and
    each part rescaled to 0-1 so access (0-2) counts as much as the others."""
    equal = dict.fromkeys(PARTS, 1.0)
    options: dict[str, dict[str, float]] = {"equal (published)": equal}
    for part in PARTS:
        options[f"without {part}"] = {**equal, part: 0.0}
    for part in PARTS:
        options[f"{part} doubled"] = {**equal, part: 2.0}
    options["each part rescaled to 0-1"] = {p: 1.0 / PART_MAXIMA[p] for p in PARTS}
    return options


def weighted(lga: ScoredLga, weights: dict[str, float]) -> float:
    return sum(weights[p] * v for p, v in zip(PARTS, lga.parts))


def top_group(lgas: list[ScoredLga], weights: dict[str, float], size: int) -> set[str]:
    """LGAs at or above the size-th highest weighted score. Ties at the cut are
    all kept, so the group can be larger than size but never depends on order."""
    if size <= 0 or not lgas:
        return set()
    scores = sorted((weighted(lga, weights) for lga in lgas), reverse=True)
    cut = scores[min(size, len(scores)) - 1]
    return {lga.label for lga in lgas if weighted(lga, weights) >= cut}


def compare(lgas: list[ScoredLga]) -> list[Comparison]:
    """Compare every weighting's top group with the published high tier."""
    high = {lga.label for lga in lgas if lga.score >= HIGH_TIER_MIN_SCORE}
    results = []
    for name, weights in weightings().items():
        group = top_group(lgas, weights, len(high))
        results.append(Comparison(name, len(group), len(group & high), len(high)))
    return results


def load(db_path: Path) -> list[ScoredLga]:
    """Read the scored LGAs from the register, read-only."""
    if not db_path.is_file():
        raise SystemExit(f"no register at {db_path}; build one first (make build) or pass --db")
    with duckdb.connect(str(db_path), read_only=True) as conn:
        rows = conn.execute(_QUERY).fetchall()
    return [ScoredLga(label, (b, c, a, m), score) for label, b, c, a, m, score in rows]


def report(lgas: list[ScoredLga]) -> str:
    high = sum(1 for lga in lgas if lga.score >= HIGH_TIER_MIN_SCORE)
    lines = [f"{len(lgas)} scored LGAs, {high} in the high tier (score {HIGH_TIER_MIN_SCORE}+)", ""]
    lines.append(f"{'weighting':<28} {'top group':>9} {'still high':>10} {'overlap':>8}")
    for result in compare(lgas):
        lines.append(f"{result.name:<28} {result.group_size:>9} {result.retained:>10} {result.overlap:>8.0%}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="register to read, relative to the repository root (default %(default)s)")
    args = parser.parse_args(argv)
    db_path = args.db if args.db.is_absolute() else ROOT / args.db
    print(report(load(db_path)))


if __name__ == "__main__":
    main(sys.argv[1:])
