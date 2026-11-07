-- Per-capita coverage on lga_coverage_burden agrees with its inputs, so a rate
-- on the dashboard can always be traced back to a count and a denominator:
-- population and births equal their raw sources for every LGA, each rate is the
-- facility count per 10,000 of its denominator, and a rate is null exactly when
-- no facility matched the LGA (a zero count there may be a join miss, so it is
-- never shown as a rate of 0). Returns one row per violation.
with c as (
    select * from {{ ref('lga_coverage_burden') }}
),

frame as (
    select
        {{ norm_state('state') }}            as state_key,
        {{ norm_lga_key('canonical_lga') }}  as lga_key,
        shapeID                              as shape_id
    from {{ source('raw', 'frame_master_index') }}
),

keyed as (
    select
        c.*,
        f.shape_id
    from c
    join frame f
      on {{ norm_state('c.state') }} = f.state_key
     and {{ norm_lga_key('c.canonical_lga') }} = f.lga_key
)

select state, canonical_lga, 'population differs from the source' as problem
from keyed k
where k.population is distinct from (
    select a.population from {{ ref('stg_access_population_lga') }} a where a.shapeID = k.shape_id
)

union all
select state, canonical_lga, 'births differ from the source'
from keyed k
where k.births is distinct from (
    select b.births from {{ source('raw', 'births_lga') }} b where b.shapeID = k.shape_id
)

union all
select state, canonical_lga, 'rate is null exactly when no facility matched: violated'
from keyed
where (facilities_per_10k_population is null) <> (facility_status = 'unmatched')
   or (facilities_per_10k_births is null) <> (facility_status = 'unmatched')

union all
select state, canonical_lga, 'rate does not recompute from facilities and its denominator'
from keyed
where abs(facilities_per_10k_population - facilities * 10000.0 / population) > 0.001
   or abs(facilities_per_10k_births - facilities * 10000.0 / births) > 0.001
