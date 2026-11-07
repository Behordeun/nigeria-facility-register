-- Access and flood columns on lga_coverage_burden agree with their inputs, so
-- every figure on the Access tab traces back to a source row: the travel-time
-- columns equal the per-LGA access table, each flood flag equals that event's
-- source row, hard_to_reach follows its threshold and is null exactly when the
-- walking share is null, and access_risk_score recomputes from its two points.
-- Returns one row per violation.
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
        f.state_key,
        f.lga_key,
        f.shape_id
    from c
    join frame f
      on {{ norm_state('c.state') }} = f.state_key
     and {{ norm_lga_key('c.canonical_lga') }} = f.lga_key
),

access as (
    select * from {{ ref('stg_access_population_lga') }}
),

flood as (
    select
        {{ norm_state('state') }}            as state_key,
        {{ norm_lga_key('canonical_lga') }}  as lga_key,
        event_year,
        affected = 1                         as affected
    from {{ source('raw', 'flood_event_affected_lga') }}
)

select k.state, k.canonical_lga, 'travel-time columns differ from the source' as problem
from keyed k
join access a on a.shapeID = k.shape_id
where k.motorised_minutes        is distinct from a.motorised_minutes
   or k.walking_minutes          is distinct from a.walking_minutes
   or k.walking_over_60min_share is distinct from a.walking_over_60min_share
   or k.access_population_share  is distinct from a.access_population_share

union all
select k.state, k.canonical_lga, 'a flood flag differs from its event row'
from keyed k
where k.flood_affected_2022_state is distinct from (
          select fl.affected from flood fl
          where fl.event_year = 2022 and fl.state_key = k.state_key and fl.lga_key = k.lga_key)
   or k.flood_affected_2024 is distinct from (
          select fl.affected from flood fl
          where fl.event_year = 2024 and fl.state_key = k.state_key and fl.lga_key = k.lga_key)

union all
select state, canonical_lga, 'hard_to_reach does not follow walking_over_60min_share >= 0.25'
from keyed
where hard_to_reach is distinct from (walking_over_60min_share >= 0.25)

union all
select state, canonical_lga, 'access_risk_score does not recompute from its points'
from keyed
where access_risk_score is distinct from (
    case when hard_to_reach is null then null
         else (case when hard_to_reach then 1 else 0 end)
            + (case when flood_affected_2024 then 1 else 0 end) end
)
