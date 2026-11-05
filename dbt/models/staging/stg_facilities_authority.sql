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

v3 as (
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
