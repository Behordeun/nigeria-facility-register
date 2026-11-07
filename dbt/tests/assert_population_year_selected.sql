-- stg_access_population_lga describes exactly one population year, for every
-- frame LGA, and that year is the one the build asked for: the population_year
-- var when set, otherwise the build's calendar year capped to the years
-- raw.access_population_lga carries. The register and every rate built on it
-- therefore describe a single, stated year. Returns one row per violation.
{% set pinned_year = var('population_year', none) %}

with s as (
    select * from {{ ref('stg_access_population_lga') }}
),

raw_years as (
    select min(population_year) as first_year, max(population_year) as last_year
    from {{ source('raw', 'access_population_lga') }}
),

expected as (
    select
    {% if pinned_year is not none %}
        cast({{ pinned_year }} as bigint)
    {% else %}
        least(greatest(cast(year(current_date) as bigint), first_year), last_year)
    {% endif %}
            as population_year
    from raw_years
)

select 'more than one population year selected' as problem
from s
having count(distinct population_year) > 1

union all
select 'selected year does not cover the 774 frame LGAs'
from s
having count(*) <> (select count(*) from {{ source('raw', 'frame_master_index') }})

union all
select distinct 'selected year is not the expected year'
from s
where population_year <> (select population_year from expected)
