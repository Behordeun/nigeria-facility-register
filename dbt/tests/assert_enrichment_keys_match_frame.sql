-- Frame join for the enrichment inputs: each must cover exactly the 774 frame
-- LGAs, no more and no fewer, before any mart consumes it. A source LGA the
-- frame does not know would attach to nothing; a frame LGA the source lacks
-- would surface later as a silent null that reads like "not measured" when the
-- input was in fact truncated.
--
-- ntd_espen_lga is checked per disease, flood_event_affected_lga per event and
-- access_population_lga per population year, so a slice that lost LGAs cannot
-- hide behind the others. LGA keys
-- are normalised on both sides (state via norm_state so FCT matches, LGA via
-- norm_lga_key); births_lga and access_population_lga are keyed on shapeID, the
-- geoBoundaries id the frame carries. Anti-joins use NOT EXISTS so a null key cannot pass silently.
-- Returns one row per (source, slice, key) that fails.
with frame as (
    select
        {{ norm_state('state') }}            as state_key,
        {{ norm_lga_key('canonical_lga') }}  as lga_key,
        shapeID                              as shape_id
    from {{ source('raw', 'frame_master_index') }}
),

espen as (
    select
        disease                              as slice,
        {{ norm_state('state') }}            as state_key,
        {{ norm_lga_key('canonical_lga') }}  as lga_key
    from {{ source('raw', 'ntd_espen_lga') }}
),

flood as (
    select
        cast(event_year as varchar)          as slice,
        {{ norm_state('state') }}            as state_key,
        {{ norm_lga_key('canonical_lga') }}  as lga_key
    from {{ source('raw', 'flood_event_affected_lga') }}
),

keyed as (
    select 'ntd_espen_lga' as source, * from espen
    union all
    select 'flood_event_affected_lga', * from flood
),

slices as (
    select distinct source, slice from keyed
),

not_in_frame as (
    select k.source, k.slice, k.state_key, k.lga_key, 'not in frame' as problem
    from keyed k
    where k.state_key is null
       or k.lga_key is null
       or not exists (
           select 1 from frame f
           where f.state_key = k.state_key and f.lga_key = k.lga_key
       )
),

missing_from_source as (
    select s.source, s.slice, f.state_key, f.lga_key, 'frame LGA missing' as problem
    from slices s
    cross join frame f
    where not exists (
        select 1 from keyed k
        where k.source = s.source and k.slice = s.slice
          and k.state_key = f.state_key and k.lga_key = f.lga_key
    )
),

births_not_in_frame as (
    select 'births_lga' as source, '' as slice, b.shapeID as state_key,
           null as lga_key, 'not in frame' as problem
    from {{ source('raw', 'births_lga') }} b
    where b.shapeID is null
       or not exists (select 1 from frame f where f.shape_id = b.shapeID)
),

births_missing as (
    select 'births_lga', '', f.shape_id, null, 'frame LGA missing'
    from frame f
    where not exists (
        select 1 from {{ source('raw', 'births_lga') }} b where b.shapeID = f.shape_id
    )
),

access_not_in_frame as (
    select 'access_population_lga' as source, cast(a.population_year as varchar) as slice,
           a.shapeID as state_key, null as lga_key, 'not in frame' as problem
    from {{ source('raw', 'access_population_lga') }} a
    where a.shapeID is null
       or not exists (select 1 from frame f where f.shape_id = a.shapeID)
),

-- Checked per population year, so a year that lost LGAs cannot hide behind the
-- complete ones: whichever year a build selects is complete.
access_years as (
    select distinct population_year from {{ source('raw', 'access_population_lga') }}
),

access_missing as (
    select 'access_population_lga', cast(y.population_year as varchar), f.shape_id, null,
           'frame LGA missing'
    from access_years y
    cross join frame f
    where not exists (
        select 1 from {{ source('raw', 'access_population_lga') }} a
        where a.shapeID = f.shape_id and a.population_year = y.population_year
    )
)

select * from not_in_frame
union all
select * from missing_from_source
union all
select * from births_not_in_frame
union all
select * from births_missing
union all
select * from access_not_in_frame
union all
select * from access_missing
