-- Version-aware authoritative facility layer.
-- v3.0 is authoritative for the 24 states it covers; v2.0 covers the rest.
-- Every row carries which version it came from and whether that state is
-- current or stale, so no consumer silently mixes two vintages.

with v3_states as (
    select distinct trim(state_standard) as state
    from {{ source('raw', 'grid3_v3') }}
),

v3 as (
    select
        unique_id                                   as facility_id,
        trim(state_standard)                        as state,
        trim(lga_standard)                          as lga,
        facility_name,
        facility_level,
        functional,
        latitude,
        longitude,
        'v3.0'                                       as source_version,
        date '2026-08-13'                            as source_date,
        'current'                                    as facility_status
    from {{ source('raw', 'grid3_v3') }}
),

v2 as (
    select
        globalid                                    as facility_id,
        trim(state)                                 as state,
        trim(lga)                                   as lga,
        facility_name,
        facility_level,
        cast(null as varchar)                       as functional,
        latitude,
        longitude,
        'v2.0'                                       as source_version,
        date '2024-10-31'                            as source_date,
        'stale-awaiting-v3'                          as facility_status
    from {{ source('raw', 'grid3_v2') }}
    where trim(state) not in (select state from v3_states)
)

select * from v3
union all
select * from v2
