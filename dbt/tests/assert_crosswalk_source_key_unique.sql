-- The alias crosswalk is keyed on (state, facility_lga): it maps one source LGA
-- spelling in one state to exactly one canonical frame LGA. If two rows share a
-- normalised (state, facility_lga) the coverage and drift joins match both, so
-- a facility fans out across two canonical LGAs (inflating counts) or takes a
-- conflicting mapping depending on join order. A null facility_lga is a fault
-- too: the marts join on facility_lga equality, which never matches null, so a
-- null-keyed alias silently maps nothing. This fails on any duplicated
-- normalised source key AND on a null facility_lga. State is normalised so two
-- spellings of one state do not hide a collision. Returns a row per offending
-- key.
with keyed as (
    select
        {{ norm_state('state') }} as state_key,
        lower(trim(facility_lga)) as facility_lga_key
    from {{ source('raw', 'lga_alias_crosswalk') }}
),

null_keys as (
    select state_key, facility_lga_key, 1 as n
    from keyed
    where facility_lga_key is null
),

dup_keys as (
    select state_key, facility_lga_key, count(*) as n
    from keyed
    where facility_lga_key is not null
    group by 1, 2
    having count(*) > 1
)

select state_key, facility_lga_key, n from null_keys
union all
select state_key, facility_lga_key, n from dup_keys
