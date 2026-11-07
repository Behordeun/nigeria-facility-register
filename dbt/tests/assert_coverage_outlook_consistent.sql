-- lga_coverage_outlook covers every frame LGA in every population year, once
-- each; its register-year rows equal the published coverage mart; facilities
-- are held fixed across years; and every rate and change recomputes, null
-- exactly where no facility matched. Returns one row per violation.
with o as (
    select * from {{ ref('lga_coverage_outlook') }}
),

years as (
    select distinct population_year from {{ source('raw', 'access_population_lga') }}
),

frame_lgas as (
    select count(*) as n from {{ source('raw', 'frame_master_index') }}
)

select cast(y.population_year as varchar) as slice, 'year does not cover every frame LGA once' as problem
from years y
left join o on o.population_year = y.population_year
group by y.population_year
having count(o.canonical_lga) <> (select n from frame_lgas)
    or count(distinct o.state || '|' || o.canonical_lga) <> (select n from frame_lgas)

union all
select 'register year', 'register-year rows are not exactly one year of 774 LGAs'
from o
having count(*) filter (where is_register_year) <> (select n from frame_lgas)
    or count(distinct population_year) filter (where is_register_year) <> 1

union all
select 'last projection year', 'last-projection-year rows are not the latest year, once per LGA'
from o
having count(*) filter (where is_last_projection_year) <> (select n from frame_lgas)
    or count(*) filter (where is_last_projection_year
                         and population_year <> (select max(population_year) from years)) > 0

union all
select o.state || '|' || o.canonical_lga, 'register-year row differs from lga_coverage_burden'
from o
join {{ ref('lga_coverage_burden') }} c using (state, canonical_lga)
where o.is_register_year
  and (o.population <> c.population
       or o.facilities <> c.facilities
       or o.facilities_per_10k_population is distinct from c.facilities_per_10k_population)

union all
select state || '|' || canonical_lga, 'facilities change across years'
from o
group by state, canonical_lga
having count(distinct facilities) <> 1

union all
select state || '|' || canonical_lga || '|' || population_year, 'rate does not recompute (null exactly when unmatched)'
from o
where (facilities_per_10k_population is null) <> (facility_status = 'unmatched')
   or abs(facilities_per_10k_population - facilities * 10000.0 / population) > 0.001

union all
select o.state || '|' || o.canonical_lga || '|' || o.population_year, 'change from the register year does not recompute'
from o
join o r on r.state = o.state and r.canonical_lga = o.canonical_lga and r.is_register_year
where abs(o.population_change_from_register_year - (o.population * 1.0 / r.population - 1)) > 0.0001
   or abs(o.facilities_per_10k_change_from_register_year
          - (o.facilities_per_10k_population - r.facilities_per_10k_population)) > 0.002
   or (o.facilities_per_10k_change_from_register_year is null) <> (o.facilities_per_10k_population is null)
