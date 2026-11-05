-- Version-aware authoritative facility layer.
-- v3.0 is authoritative for the 24 states it covers; v2.0 covers the rest.
-- Every row carries which version it came from and whether that state is
-- current or stale, so no consumer silently mixes two vintages.
--
-- State labels are canonicalised (norm_state) before the switch, because v2 and
-- v3 can spell the same state differently; without that a covered state would
-- keep v2 rows beside v3. Null/blank states are excluded from the v3 coverage
-- set so a stray null there cannot, through NOT IN, drop every v2 fallback row.

with v3_states as (
    select distinct {{ norm_state('state_standard') }} as state
    from {{ source('raw', 'grid3_v3') }}
    where state_standard is not null
      and trim(state_standard) <> ''
),

-- v3.0 ships a small number of exact-duplicate records: the same unique_id
-- repeated with every other field identical (seen on un-geocoded rows, where
-- the derived unique_id has no coordinate to separate it). GRID3's own release
-- notes document a duplicate-detection pass, so these are a known property of
-- the source, not a load error. Collapse them to one row per facility_id here
-- so the authoritative layer keeps the unique-id grain the rest of the pipeline
-- relies on. row_number over the full row keeps the choice deterministic, and
-- because the duplicates are byte-identical, dropping the copies loses nothing.
-- A non-identical collision under one unique_id (distinct facilities sharing an
-- id) is a different, harder case the dedup must not silently absorb; the guard
-- test assert_v3_duplicate_ids_are_identical fails the build if one appears.
v3_raw as (
    select
        unique_id                                   as facility_id,
        {{ norm_state('state_standard') }}          as state,
        trim(lga_standard)                          as lga,
        facility_name,
        facility_level,
        functional,
        latitude,
        longitude,
        'v3.0'                                       as source_version,
        date '2025-12-31'                            as source_date,
        'current'                                    as facility_status
    from {{ source('raw', 'grid3_v3') }}
),

v3 as (
    select
        facility_id, state, lga, facility_name, facility_level,
        functional, latitude, longitude,
        source_version, source_date, facility_status
    from v3_raw
    -- Most repeats are byte-identical. One id carries two rows that agree on
    -- every identity field (name, lga, coordinates) but disagree only on
    -- functional, where one says Unknown. Prefer a known status over Unknown so
    -- the kept row is the more informative one, not whichever the sort happens
    -- to surface. Beyond that tiebreak the order is the full row, so the choice
    -- stays deterministic. Any duplicate that differs on an identity field is a
    -- distinct facility under one id, which this must not silently drop; the
    -- guard test fails the build before that can happen.
    qualify row_number() over (
        partition by facility_id
        order by
            case when functional is null or functional = 'Unknown' then 1 else 0 end,
            state, lga, facility_name, facility_level,
            functional, latitude, longitude
    ) = 1
),

v2 as (
    select
        globalid                                    as facility_id,
        {{ norm_state('state') }}                   as state,
        trim(lga)                                   as lga,
        facility_name,
        facility_level,
        cast(null as varchar)                       as functional,
        latitude,
        longitude,
        'v2.0'                                       as source_version,
        date '2024-10-31'                            as source_date,
        'stale-awaiting-v3'                          as facility_status
    from {{ source('raw', 'grid3_v2') }} v2src
    where {{ norm_state('v2src.state') }} not in (select state from v3_states)
)

select * from v3
union all
select * from v2
