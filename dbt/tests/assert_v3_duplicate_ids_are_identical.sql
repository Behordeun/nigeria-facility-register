-- Guards the v3 dedup in stg_facilities_authority. That model collapses repeated
-- v3 unique_ids to one row. Most repeats are byte-identical, and one differs
-- only on functional, which the model resolves deterministically by preferring a
-- known status over Unknown. So functional is excluded from the comparison here:
-- a difference the model already resolves is not a failure. Every other field is
-- an identity field (state, lga, name, level, coordinates); if two rows share an
-- id but disagree on any of those, they are distinct facilities under one id and
-- the dedup would silently discard a real record. This fails the build if that
-- happens, so the dedup can only ever drop a status-only or exact duplicate, not
-- a genuinely different facility. Returns a row only on failure.
with v3 as (
    select
        unique_id                               as facility_id,
        {{ norm_state('state_standard') }}      as state,
        trim(lga_standard)                      as lga,
        facility_name,
        facility_level,
        latitude,
        longitude
    from {{ source('raw', 'grid3_v3') }}
),

distinct_identity_rows_per_id as (
    select
        facility_id,
        count(*) as distinct_identity_variants
    from (
        select distinct
            facility_id, state, lga, facility_name,
            facility_level, latitude, longitude
        from v3
    )
    group by facility_id
)

select facility_id, distinct_identity_variants
from distinct_identity_rows_per_id
where distinct_identity_variants > 1
