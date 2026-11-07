-- Mart: one row describing the published register as a whole.
-- The headline figures a reader needs to judge what they are looking at: when
-- the register was built, which GRID3 release each part comes from, how much of
-- the LGA frame each source covers, and the lineage totals. A dashboard renders
-- this row as prose, so every figure is computed here, tested, and refreshed
-- with the data rather than typed into a report.
--
-- built_at is the build time of this table, which is the refresh that published
-- the register (a refresh builds off to the side and promotes the whole file).

with coverage as (
    select * from {{ ref('lga_coverage_burden') }}
),

authority as (
    select source_version, max(source_date) as source_date, count(*) as facilities
    from {{ ref('stg_facilities_authority') }}
    group by source_version
),

states as (
    -- A state's facilities all come from one version, so any_value is exact.
    select state, any_value(facility_source_version) as version
    from coverage
    where facility_source_version is not null
    group by state
),

ntd as (
    select
        count(*) filter (where disease_code = 'STH'   and endemicity_rank is not null) as sth_known_lgas,
        count(*) filter (where disease_code = 'SCH'   and endemicity_rank is not null) as sch_known_lgas,
        count(*) filter (where disease_code = 'lf'    and endemicity_rank is not null) as lf_known_lgas,
        count(*) filter (where disease_code = 'Oncho' and endemicity_rank is not null) as oncho_known_lgas,
        count(*) filter (where disease_code = 'Loa'   and endemicity_rank is not null) as loa_known_lgas,
        max(year_latest)                                                                as espen_latest_year
    from {{ ref('stg_ntd_endemicity_lga') }}
),

lineage as (
    select
        count(*) filter (where lineage_status = 'carried-over') as lineage_carried_over,
        count(*) filter (where lineage_status = 'dropped')      as lineage_dropped,
        count(*) filter (where lineage_status = 'appeared')     as lineage_appeared
    from {{ ref('lga_version_drift') }}
)

select
    cast(current_timestamp as timestamp)                                         as built_at,
    (select count(*) from coverage)                                              as lgas,
    (select count(distinct state) from coverage)                                 as states,
    (select count(*) from states where version = 'v3.0')                         as states_on_v3,
    (select count(*) from states where version = 'v2.0')                         as states_on_v2,
    -- A state none of whose LGAs matched a facility has no version: an absence
    -- to reconcile, counted here rather than folded into either version.
    (select count(distinct state) from coverage) - (select count(*) from states) as states_without_facilities,
    (select source_date from authority where source_version = 'v3.0')            as v3_source_date,
    (select source_date from authority where source_version = 'v2.0')            as v2_source_date,
    -- DuckDB widens sum(bigint) to hugeint, so the sums are cast back to the
    -- contract type.
    cast((select coalesce(sum(facilities), 0) from authority) as bigint)                         as facilities,
    -- Facilities whose LGA resolved to a frame LGA; the gap to `facilities` is
    -- the facilities the crosswalk could not place, kept visible, not hidden.
    cast((select coalesce(sum(facilities), 0) from coverage) as bigint)                          as facilities_in_lgas,
    cast((select coalesce(sum(facilities), 0) from authority where source_version = 'v3.0') as bigint) as facilities_v3,
    cast((select coalesce(sum(facilities), 0) from authority where source_version = 'v2.0') as bigint) as facilities_v2,
    (select count(*) from coverage where facility_status = 'unmatched')          as lgas_unmatched,
    (select count(*) from coverage where priority_high_burden_thin_coverage)     as priority_lgas,
    (select count(*) from coverage where priority_tier = 'high')                 as priority_high_tier_lgas,
    cast((select sum(population) from coverage) as bigint)                       as population,
    (select max(population_year) from coverage)                                  as population_year,
    (select count(*) from coverage where hard_to_reach)                          as hard_to_reach_lgas,
    cast((select coalesce(sum(facilities_secondary + facilities_tertiary), 0) from coverage) as bigint) as facilities_secondary_tertiary,
    cast((select coalesce(sum(facilities_level_unknown), 0) from coverage) as bigint)                  as facilities_level_unknown,
    cast((select coalesce(sum(facilities_functional + facilities_not_functional), 0) from coverage) as bigint) as facilities_functional_status_known,
    cast((select coalesce(sum(facilities_not_functional), 0) from coverage) as bigint)                 as facilities_not_functional,
    (select count(*) from coverage where flood_affected_2024)                    as flood_affected_2024_lgas,
    ntd.sth_known_lgas,
    ntd.sch_known_lgas,
    ntd.lf_known_lgas,
    ntd.oncho_known_lgas,
    ntd.loa_known_lgas,
    ntd.espen_latest_year,
    lineage.lineage_carried_over,
    lineage.lineage_dropped,
    lineage.lineage_appeared
from ntd, lineage
