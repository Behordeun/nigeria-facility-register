-- Staging: per-LGA population and travel time for ONE population year.
--
-- raw.access_population_lga holds one row per LGA per year (WorldPop R2025A
-- publishes 2015-2030; the committed table carries every pinned year). This
-- model picks the year the published register describes, so the marts and
-- their tests read a single, consistent year:
--
-- - the `population_year` var when set (a reproducible build of a given year);
--   a year the table does not carry fails the build rather than falling back;
-- - otherwise the calendar year of the build, capped to the years available,
--   so a weekly refresh moves to the new year on 1 January without fetching
--   anything, and stays on the last published year once the table runs out.
--
-- The selected year is carried on every row (population_year) and surfaces in
-- register_summary and the dashboard's Data status page.

{% set pinned_year = var('population_year', none) %}

with source as (
    select * from {{ source('raw', 'access_population_lga') }}
),

bounds as (
    select min(population_year) as first_year, max(population_year) as last_year
    from source
),

selected as (
    select
    {% if pinned_year is not none %}
        -- Unknown year: error() aborts the build with a clear message.
        case
            when {{ pinned_year }} between b.first_year and b.last_year
             and exists (select 1 from source where population_year = {{ pinned_year }})
            then {{ pinned_year }}
            else error('population_year {{ pinned_year }} is not in raw.access_population_lga')
        end
    {% else %}
        least(greatest(cast(year(current_date) as bigint), b.first_year), b.last_year)
    {% endif %}
            as population_year
    from bounds b
)

select
    s.shapeID,
    s.shapeName,
    s.population_year,
    s.population,
    s.motorised_minutes,
    s.walking_minutes,
    s.walking_over_60min_share,
    s.access_population_share
from source s
join selected using (population_year)
