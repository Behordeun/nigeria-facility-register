-- Mart: per-LGA coverage across every WorldPop population year the register
-- carries (2025-2030), holding today's facility count fixed. It answers how
-- per-capita coverage erodes as population grows if no facility is added: one
-- row per (LGA, population_year), 774 x the number of years.
--
-- Facilities and the matched/unmatched status come from lga_coverage_burden,
-- and the register's own year from stg_access_population_lga, so the row for
-- the register year carries exactly the population and rate the published
-- register shows. A rate is null, never 0, where no facility matched the LGA,
-- by the same rule as the coverage mart. Projections are WorldPop's; the
-- facility count is today's, not a forecast.

with coverage as (
    select
        state,
        canonical_lga,
        facilities,
        facility_status,
        {{ norm_state('state') }}            as state_key,
        {{ norm_lga_key('canonical_lga') }}  as lga_key
    from {{ ref('lga_coverage_burden') }}
),

frame as (
    select
        shapeID                              as shape_id,
        {{ norm_state('state') }}            as state_key,
        {{ norm_lga_key('canonical_lga') }}  as lga_key
    from {{ source('raw', 'frame_master_index') }}
),

register_year as (
    select any_value(population_year) as population_year
    from {{ ref('stg_access_population_lga') }}
),

-- The last projection year is flagged in the data so a dashboard can filter on
-- it without a sub-query (Superset refuses sub-queries in custom SQL filters).
last_year as (
    select max(population_year) as population_year
    from {{ source('raw', 'access_population_lga') }}
),

population as (
    select shapeID as shape_id, population_year, population
    from {{ source('raw', 'access_population_lga') }}
),

by_year as (
    select
        c.state,
        c.canonical_lga,
        p.population_year,
        p.population,
        c.facilities,
        c.facility_status,
        case when c.facility_status = 'unmatched' then null
             else round(cast(c.facilities as double) * 10000 / p.population, 3) end
            as facilities_per_10k_population
    from coverage c
    join frame f using (state_key, lga_key)
    join population p on p.shape_id = f.shape_id
),

at_register_year as (
    select state, canonical_lga, population as population_register_year,
           facilities_per_10k_population as facilities_per_10k_register_year
    from by_year
    where population_year = (select population_year from register_year)
)

select
    b.state,
    b.canonical_lga,
    b.population_year,
    b.population_year = (select population_year from register_year) as is_register_year,
    b.population_year = (select population_year from last_year)     as is_last_projection_year,
    b.population,
    b.facilities,
    b.facility_status,
    b.facilities_per_10k_population,
    round(cast(b.population as double) / r.population_register_year - 1, 4)
        as population_change_from_register_year,
    round(b.facilities_per_10k_population - r.facilities_per_10k_register_year, 3)
        as facilities_per_10k_change_from_register_year
from by_year b
join at_register_year r using (state, canonical_lga)
