-- Facility mix on lga_coverage_burden partitions the facility count and its
-- rates recompute from their parts, so a level or functionality figure on the
-- dashboard always adds up to the facilities it describes: the four level
-- counts sum to facilities, the three status counts sum to facilities, a v2.0
-- LGA carries no recorded status (GRID3 v2.0 does not record one), each rate
-- recomputes, and each is null exactly where its denominator is not a
-- measurement. Returns one row per violation.
with c as (
    select * from {{ ref('lga_coverage_burden') }}
)

select state, canonical_lga, 'level counts do not sum to facilities' as problem
from c
where facilities_primary + facilities_secondary + facilities_tertiary + facilities_level_unknown <> facilities

union all
select state, canonical_lga, 'functional status counts do not sum to facilities'
from c
where facilities_functional + facilities_not_functional + facilities_status_unknown <> facilities

union all
select state, canonical_lga, 'a count is negative'
from c
where least(facilities_primary, facilities_secondary, facilities_tertiary, facilities_level_unknown,
            facilities_functional, facilities_not_functional, facilities_status_unknown) < 0

union all
select state, canonical_lga, 'a v2.0 LGA carries a recorded functional status'
from c
where facility_source_version = 'v2.0'
  and facilities_functional + facilities_not_functional > 0

union all
select state, canonical_lga, 'secondary_tertiary_per_100k_population does not recompute (null exactly when unmatched)'
from c
where (secondary_tertiary_per_100k_population is null) <> (facility_status = 'unmatched')
   or abs(secondary_tertiary_per_100k_population
          - (facilities_secondary + facilities_tertiary) * 100000.0 / population) > 0.001

union all
select state, canonical_lga, 'functional_status_known_share does not recompute (null exactly when unmatched)'
from c
where (functional_status_known_share is null) <> (facility_status = 'unmatched')
   or abs(functional_status_known_share
          - (facilities_functional + facilities_not_functional) * 1.0 / facilities) > 0.0001

union all
select state, canonical_lga, 'not_functional_share does not recompute (null exactly when no status is known)'
from c
where (not_functional_share is null) <> (facilities_functional + facilities_not_functional = 0)
   or abs(not_functional_share
          - facilities_not_functional * 1.0 / nullif(facilities_functional + facilities_not_functional, 0)) > 0.0001
   or not_functional_share < 0 or not_functional_share > 1
