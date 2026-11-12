"""
What changed between the outgoing register and the one being published.

The comparison is made at publish time, the only moment both registers exist:
the staged build is complete and the live register has not yet been replaced.
It cannot be a dbt model, because dbt builds each staging database from scratch
with no access to the previous vintage. The result is written into the staging
database as `register_changes`, so it is promoted with the register it describes
and a restored register keeps its own record.

One row per change, in display order:

  - a summary row: the previous and the new input fingerprints, and how many
    LGAs changed priority tier, facility count and match status;
  - every LGA whose priority tier changed;
  - every LGA that newly matched a facility or no longer matches any;
  - the LGAs whose facility count changed most (FACILITY_CHANGES_LISTED of them).

On a first publish, or when the outgoing register predates the columns compared
here, only the summary row is written and its detail says why.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

TABLE = "register_changes"
FACILITY_CHANGES_LISTED = 25
# The columns of the outgoing register's coverage mart that are compared. A
# register without all of them cannot be compared and gets a summary row only.
COMPARED_COLUMNS = ("state", "canonical_lga", "priority_tier", "facilities", "facility_status")
# The published table's columns and types, in order. The bundle test reads this
# in place of a dbt contract, because this table is not built by dbt.
COLUMNS = {
    "change_order": "INTEGER",
    "change": "VARCHAR",
    "lga_label": "VARCHAR",
    "state": "VARCHAR",
    "canonical_lga": "VARCHAR",
    "previous": "VARCHAR",
    "current": "VARCHAR",
    "facilities_change": "BIGINT",
    "detail": "VARCHAR",
}

_CREATE = (
    'create or replace table "register_changes" ('
    "change_order integer not null, change varchar not null, lga_label varchar, state varchar, "
    "canonical_lga varchar, previous varchar, current varchar, facilities_change bigint, detail varchar)"
)
_CREATE_PREVIOUS = (
    "create temp table previous_lgas (state varchar, canonical_lga varchar, priority_tier varchar, "
    "facilities bigint, facility_status varchar)"
)
_INSERT_SUMMARY = 'insert into "register_changes" values (0, \'summary\', null, null, null, ?, ?, null, ?)'

# The comparison, numbering rows so the dashboard shows them in a fixed order:
# tier changes (to high first), then match changes, then the largest facility
# moves. LGAs are matched on (state, canonical_lga), the coverage mart's key.
_INSERT_CHANGES = """
insert into "register_changes"
with paired as (
    select c.state, c.canonical_lga, c.lga_label,
           p.priority_tier as previous_tier, c.priority_tier as current_tier,
           p.facilities as previous_facilities, c.facilities as current_facilities,
           p.facility_status = 'unmatched' as previously_unmatched,
           c.facility_status = 'unmatched' as now_unmatched
    from lga_coverage_burden c
    join previous_lgas p using (state, canonical_lga)
),
tier as (
    select 'priority tier' as change, lga_label, state, canonical_lga,
           previous_tier as previous, current_tier as current, null::bigint as facilities_change,
           case current_tier when 'high' then 0 when 'medium' then 1 else 2 end as rank_1,
           0 as rank_2
    from paired where previous_tier is distinct from current_tier
),
matched as (
    select 'facility match' as change, lga_label, state, canonical_lga,
           case when previously_unmatched then 'no facility' else 'facilities matched' end,
           case when now_unmatched then 'no facility' else 'facilities matched' end,
           current_facilities - previous_facilities, 0, 0
    from paired where previously_unmatched is distinct from now_unmatched
),
moved as (
    select 'facilities' as change, lga_label, state, canonical_lga,
           previous_facilities::varchar, current_facilities::varchar,
           current_facilities - previous_facilities,
           0, -abs(current_facilities - previous_facilities)
    from paired where previous_facilities is distinct from current_facilities
    order by abs(current_facilities - previous_facilities) desc, lga_label
    limit ?
),
listed as (
    select *, 1 as section from tier
    union all select *, 2 from matched
    union all select *, 3 from moved
)
select (row_number() over (order by section, rank_1, rank_2, lga_label))::integer,
       change, lga_label, state, canonical_lga, previous, current, facilities_change, null
from listed
"""
_COUNTS = """
select count(*) filter (where p.priority_tier is distinct from c.priority_tier),
       count(*) filter (where (p.facility_status = 'unmatched') is distinct from (c.facility_status = 'unmatched')),
       count(*) filter (where p.facilities is distinct from c.facilities)
from lga_coverage_burden c join previous_lgas p using (state, canonical_lga)
"""


def write_register_changes(staging_db: Path, live_db: Path, current_fingerprint: str,
                           previous_fingerprint: str | None) -> None:
    """Write `register_changes` into the staging database, comparing it with the live register.

    Raises if the staging database cannot be written, which fails the publish
    before the register is promoted. An outgoing register that is missing or
    cannot be compared is not an error: the summary row records why.
    """
    previous_rows, reason = _previous_lgas(live_db)
    con = duckdb.connect(str(staging_db))
    try:
        con.execute("begin")
        con.execute(_CREATE)
        if previous_rows is None:
            con.execute(_INSERT_SUMMARY, [previous_fingerprint, current_fingerprint, reason])
        else:
            con.execute(_CREATE_PREVIOUS)
            con.executemany("insert into previous_lgas values (?, ?, ?, ?, ?)", previous_rows)
            tiers, matches, moves = con.execute(_COUNTS).fetchone()
            con.execute(_INSERT_SUMMARY, [previous_fingerprint, current_fingerprint,
                                          _summary(tiers, matches, moves)])
            con.execute(_INSERT_CHANGES, [FACILITY_CHANGES_LISTED])
        con.execute("commit")
    finally:
        con.close()


def _previous_lgas(live_db: Path) -> tuple[list[tuple] | None, str]:
    """The outgoing register's compared columns, or None and the reason there are none."""
    if not live_db.is_file():
        return None, "first publish: there is no previous register to compare with"
    con = duckdb.connect(str(live_db), read_only=True)
    try:
        present = {row[0] for row in con.execute(
            "select column_name from information_schema.columns where table_name = 'lga_coverage_burden'"
        ).fetchall()}
        if not set(COMPARED_COLUMNS) <= present:
            return None, "the previous register predates these comparisons, so only this summary is recorded"
        rows = con.execute(
            "select state, canonical_lga, priority_tier, facilities, facility_status from lga_coverage_burden"
        ).fetchall()
    finally:
        con.close()
    return rows, ""


def _summary(tiers: int, matches: int, moves: int) -> str:
    listed = min(moves, FACILITY_CHANGES_LISTED)
    return (f"{tiers} LGA(s) changed priority tier; {matches} changed whether any facility matches; "
            f"{moves} changed facility count ({listed} largest listed).")
