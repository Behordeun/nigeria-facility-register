-- Grain of the enrichment inputs: one row per (LGA, disease) in ntd_espen_lga
-- and one per (LGA, event_year) in flood_event_affected_lga. A duplicate would
-- multiply an LGA when a mart joins it, the same failure assert_ntd_state_unique
-- guards on the state table. Keys are normalised so two spellings of one LGA
-- cannot pass as distinct. births_lga uniqueness on shapeID is a schema test.
-- Returns one row per duplicated key.
select
    'ntd_espen_lga'                      as source,
    disease                              as slice,
    {{ norm_state('state') }}            as state_key,
    {{ norm_lga_key('canonical_lga') }}  as lga_key,
    count(*)                             as n
from {{ source('raw', 'ntd_espen_lga') }}
group by 1, 2, 3, 4
having count(*) > 1

union all

select
    'flood_event_affected_lga',
    cast(event_year as varchar),
    {{ norm_state('state') }},
    {{ norm_lga_key('canonical_lga') }},
    count(*)
from {{ source('raw', 'flood_event_affected_lga') }}
group by 1, 2, 3, 4
having count(*) > 1
