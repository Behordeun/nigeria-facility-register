"""
Run metrics read from the built DuckDB, kept separate from the Dagster wiring.

These functions take an open DuckDB connection and return plain numbers, so the
same queries that feed Dagster run metadata and asset checks can be unit-tested
against the sample fixture without a Dagster context. Each metric is a question
the pipeline should be able to answer about a refresh: how much loaded, how much
of it resolved to the canonical frame, and how much of the previous vintage
carried over.

A rate is returned as None when its denominator is zero, never as a misleading
0.0: "no facilities to resolve" is not "nothing resolved". Callers render None
as "n/a" rather than asserting a floor against it.
"""
from __future__ import annotations

from dataclasses import dataclass

import duckdb


@dataclass(frozen=True)
class RunMetrics:
    """One refresh's observability figures.

    Counts are absolute; rates are fractions in [0, 1] or None when the
    denominator is zero. carryover_rate is carried-over over the facilities that
    could carry over (carried-over plus dropped), i.e. the v2.0 facilities in a
    v3-covered state; appeared facilities are not in that denominator because
    they had no v2.0 record to carry.
    """

    raw_v2_rows: int
    raw_v3_rows: int
    authority_rows: int
    lineage_rows: int
    coverage_lga_resolve_rate: float | None
    drift_lga_resolve_rate: float | None
    carryover_rate: float | None


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _norm_state(col: str) -> str:
    """SQL that canonicalises a state label, mirroring the norm_state dbt macro
    and nfr.keys.norm_state.

    The coverage mart carries the frame's state spelling (Federal Capital
    Territory) while the authority layer carries the normalised form (FCT), so a
    join or filter that compares the two raw drops FCT from both sides. Applying
    this to both sides before comparing keeps FCT (and Nassarawa/Nasarawa) in the
    counts. Keep the three in step: this macro, the Jinja macro, and the Python
    helper.
    """
    c = f"trim({col})"
    return (
        "case "
        f"when {c} in ('FCT, Abuja', 'Fct', 'Fct, Abuja', "
        "'Federal Capital Territory', 'Abuja') then 'FCT' "
        f"when {c} = 'Nassarawa' then 'Nasarawa' "
        f"else {c} end"
    )


def raw_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Row counts for the raw inputs only.

    Read before dbt runs (by the raw_sources asset), so it must touch only the
    raw tables the loader created; the staging tables do not exist yet and
    querying them here would fail the refresh before dbt builds them.
    """
    return {
        "raw_v2_rows": _scalar(con, "select count(*) from grid3_v2"),
        "raw_v3_rows": _scalar(con, "select count(*) from grid3_v3"),
    }


def load_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Absolute row counts for the raw inputs and the modelled layers.

    Reads the staging tables, so it is valid only after dbt has built them (the
    dbt_models asset). Use raw_counts before the build.
    """
    return {
        **raw_counts(con),
        "authority_rows": _scalar(con, "select count(*) from stg_facilities_authority"),
        "lineage_rows": _scalar(con, "select count(*) from stg_version_lineage"),
    }


def coverage_lga_resolve_rate(con: duckdb.DuckDBPyConnection) -> float | None:
    """Share of LGAs that matched a facility, among LGAs in states the vintage
    actually loaded.

    An unresolved LGA (facility_status = 'unmatched') is one the facility data
    did not reach, which may be a real gap or a name-join miss. The denominator
    is restricted to states present in the authority layer, not all 774 LGAs:
    otherwise the rate would measure how many states the vintage covers
    (breadth) rather than how well the covered states resolved (quality), and a
    partial vintage or a two-state fixture would read as a near-zero resolve
    even when every loaded LGA matched. Scoping to loaded states makes the rate
    comparable across a two-state sample and a full national load.
    """
    # Normalise the state on both sides before comparing: the coverage mart
    # carries the frame spelling and the authority layer the normalised form, so
    # a raw comparison would silently drop FCT from both counts.
    loaded_states = (
        f"(select distinct {_norm_state('state')} from stg_facilities_authority)"
    )
    cov_state = _norm_state("state")
    total = _scalar(
        con,
        f"select count(*) from lga_coverage_burden where {cov_state} in {loaded_states}",
    )
    resolved = _scalar(
        con,
        "select count(*) from lga_coverage_burden "
        f"where {cov_state} in {loaded_states} and facility_status <> 'unmatched'",
    )
    return _rate(resolved, total)


def drift_lga_resolve_rate(con: duckdb.DuckDBPyConnection) -> float | None:
    """Share of lineage records whose source LGA resolved to a frame LGA.

    A null canonical_lga is a source label the crosswalk does not map; this rate
    is how much of the lineage is attributable to a canonical LGA.
    """
    total = _scalar(con, "select count(*) from lga_version_drift")
    resolved = _scalar(
        con, "select count(*) from lga_version_drift where canonical_lga is not null"
    )
    return _rate(resolved, total)


def carryover_rate(con: duckdb.DuckDBPyConnection) -> float | None:
    """Carried-over facilities over the v2.0 facilities that could carry forward.

    The denominator is every v2.0 facility in a v3-covered state, counted from
    the raw table before the lineage model applies its id and coordinate guards,
    NOT carried-over-plus-dropped read back from the lineage table. Those two are
    equal only when no facility is filtered out: a vintage that loses the
    globalid or the coordinates a facility needs to enter the match drops it from
    both the carried and the dropped count, so carried/(carried+dropped) would
    stay high and hide the loss. Measuring against the expected population makes
    a facility lost to a bad load show up as a fall in the rate, which is what
    the carry-over check is meant to catch.

    A v3-covered state is one that appears in the raw v3 table; v2.0 facilities
    outside those states are not comparable (they have no v3 to carry into) and
    are excluded from the denominator.
    """
    carried = _scalar(
        con, "select count(*) from stg_version_lineage where lineage_status = 'carried-over'"
    )
    v3_states = (
        f"(select distinct {_norm_state('state_standard')} from grid3_v3 "
        "where state_standard is not null and trim(state_standard) <> '')"
    )
    expected = _scalar(
        con,
        f"select count(*) from grid3_v2 where {_norm_state('state')} in {v3_states}",
    )
    return _rate(carried, expected)


def collect(con: duckdb.DuckDBPyConnection) -> RunMetrics:
    """Read every metric for one refresh from an open connection."""
    counts = load_counts(con)
    return RunMetrics(
        raw_v2_rows=counts["raw_v2_rows"],
        raw_v3_rows=counts["raw_v3_rows"],
        authority_rows=counts["authority_rows"],
        lineage_rows=counts["lineage_rows"],
        coverage_lga_resolve_rate=coverage_lga_resolve_rate(con),
        drift_lga_resolve_rate=drift_lga_resolve_rate(con),
        carryover_rate=carryover_rate(con),
    )


def _with_connection(db_path: str, fn):
    """Open the DuckDB file read-only, run fn(con), always close."""
    con = duckdb.connect(db_path, read_only=True)
    try:
        return fn(con)
    finally:
        con.close()


def collect_from_path(db_path: str) -> RunMetrics:
    """Open the DuckDB file read-only and collect every metric."""
    return _with_connection(db_path, collect)


def raw_counts_from_path(db_path: str) -> dict[str, int]:
    """Open the DuckDB file read-only and read the raw-input counts (pre-dbt)."""
    return _with_connection(db_path, raw_counts)


def load_counts_from_path(db_path: str) -> dict[str, int]:
    """Open the DuckDB file read-only and read the load counts (post-dbt)."""
    return _with_connection(db_path, load_counts)


def coverage_lga_resolve_rate_from_path(db_path: str) -> float | None:
    """Open the DuckDB file read-only and read the coverage resolve rate."""
    return _with_connection(db_path, coverage_lga_resolve_rate)


def carryover_rate_from_path(db_path: str) -> float | None:
    """Open the DuckDB file read-only and read the carry-over rate."""
    return _with_connection(db_path, carryover_rate)
