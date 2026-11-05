-- The alias crosswalk is keyed on (state, facility_lga): it maps one source LGA
-- spelling in one state to exactly one canonical frame LGA. If two rows share a
-- normalised (state, facility_lga) the coverage and drift joins match both, so
-- a facility fans out across two canonical LGAs (inflating counts) or takes a
-- conflicting mapping depending on join order. This fails on any normalised
-- source key that appears more than once, so a duplicate or conflicting alias
-- is caught before it corrupts a count. State is normalised so two spellings of
-- one state do not hide a collision. Returns a row per duplicated key.
select
    {{ norm_state('state') }} as state_key,
    lower(trim(facility_lga))  as facility_lga_key,
    count(*)                   as n
from {{ source('raw', 'lga_alias_crosswalk') }}
group by 1, 2
having count(*) > 1
