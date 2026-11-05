-- Cross-version facility lineage: how a v2.0 facility maps to v3.0.
-- The two versions share no carry-over id (v2 keys on globalid, v3 on
-- unique_id) and the NHFR bridge code is too sparse in v3.0 to join on, so a
-- facility's lineage is reconstructed by position and name.
--
-- Match rule: a v2 facility and a v3 facility in the same canonical state are
-- candidates when they sit within 100 m of each other. Distance is a pure-SQL
-- haversine on lat/lon, not a spatial-extension call, so the match is
-- deterministic and carries no optional dependency into CI. Among candidates
-- the pairing is a single mutual claim: each v3 facility is claimed by its one
-- nearest v2, and each v2 keeps only the pairing where it is also that v3's
-- nearest, so no facility appears in two lineage records.
--
-- Classification, one record per facility:
--   carried-over : a v2 facility matched a v3 facility (keyed by the v3 id)
--   dropped      : a v2 facility matched nothing in v3 (keyed by the v2 id)
--   appeared     : a v3 facility matched no v2 facility (keyed by the v3 id)
-- "dropped" and "appeared" are review candidates, not asserted truths: a drop
-- may be a real closure or a sub-100 m move the match missed, and an appearance
-- may be new or a re-survey of the same site. The name-similarity band on a
-- carried-over record flags pairs whose names diverge for the same reason.
--
-- Only states that v3.0 actually covers can have lineage: a state still on v2.0
-- has no v3 to compare against, and every v2 row there would read as a false
-- "dropped". Those states are excluded here and surface through the authority
-- layer's 'stale-awaiting-v3' status instead.

{% set earth_radius_m = 6371000 %}
{% set match_radius_m = 100 %}

with v3_states as (
    select distinct {{ norm_state('state_standard') }} as state
    from {{ source('raw', 'grid3_v3') }}
    where state_standard is not null
      and trim(state_standard) <> ''
),

v2 as (
    select
        globalid                            as v2_id,
        {{ norm_state('state') }}           as state,
        trim(lga)                           as lga,
        facility_name                       as v2_name,
        latitude                            as v2_lat,
        longitude                           as v2_lon
    from {{ source('raw', 'grid3_v2') }}
    where globalid is not null
      and latitude is not null
      and longitude is not null
      and {{ norm_state('state') }} in (select state from v3_states)
),

v3 as (
    select
        unique_id                           as v3_id,
        {{ norm_state('state_standard') }}  as state,
        trim(lga_standard)                  as lga,
        facility_name                       as v3_name,
        latitude                            as v3_lat,
        longitude                           as v3_lon
    from {{ source('raw', 'grid3_v3') }}
    where unique_id is not null
      and latitude is not null
      and longitude is not null
      and {{ norm_state('state_standard') }} is not null
      and trim(state_standard) <> ''
),

-- Every v2/v3 pair in the same state within the match radius. The haversine is
-- expanded inline once here; distance in metres.
candidates as (
    select
        v2.v2_id,
        v3.v3_id,
        v3.state                            as state,
        v3.lga                              as v3_lga,
        v2.v2_name,
        v3.v3_name,
        {{ earth_radius_m }} * 2 * asin(sqrt(
            pow(sin(radians(v3.v3_lat - v2.v2_lat) / 2), 2)
            + cos(radians(v2.v2_lat)) * cos(radians(v3.v3_lat))
              * pow(sin(radians(v3.v3_lon - v2.v2_lon) / 2), 2)
        )) as distance_m
    from v2
    join v3 on v2.state = v3.state
    where abs(v3.v3_lat - v2.v2_lat) < 0.002        -- ~220 m lat pre-filter
      and abs(v3.v3_lon - v2.v2_lon) < 0.002
),

within_radius as (
    select * from candidates where distance_m <= {{ match_radius_m }}
),

-- Each v3 facility's single nearest v2 candidate.
v3_nearest as (
    select
        v2_id, v3_id, state, v3_lga, v2_name, v3_name, distance_m
    from (
        select *,
            row_number() over (partition by v3_id order by distance_m, v2_id) as rn
        from within_radius
    )
    where rn = 1
),

-- Each v2 facility's single nearest v3 candidate.
v2_nearest as (
    select v2_id, v3_id as v2_best_v3
    from (
        select v2_id, v3_id,
            row_number() over (partition by v2_id order by distance_m, v3_id) as rn
        from within_radius
    )
    where rn = 1
),

-- A pairing is accepted only when the two facilities are each other's nearest,
-- so no v2 and no v3 is ever claimed twice.
matched as (
    select
        n.v2_id,
        n.v3_id,
        n.state,
        n.v3_lga                            as lga,
        n.v2_name,
        n.v3_name,
        n.distance_m
    from v3_nearest n
    join v2_nearest m on n.v2_id = m.v2_id and n.v3_id = m.v2_best_v3
),

-- A null-safe anti-join (not exists, never not in): a null in the matched set
-- cannot turn the predicate unknown and silently empty the dropped/appeared
-- sets. Source ids are also filtered non-null at the v2/v3 CTEs, so a row that
-- cannot be keyed never reaches the classification.
dropped as (
    select v2.v2_id, v2.state, v2.lga, v2.v2_name
    from v2
    where not exists (select 1 from matched m where m.v2_id = v2.v2_id)
),

appeared as (
    select v3.v3_id, v3.state, v3.lga, v3.v3_name
    from v3
    where not exists (select 1 from matched m where m.v3_id = v3.v3_id)
),

lineage as (
    select
        cast(v3_id as varchar)              as facility_id,
        'carried-over'                      as lineage_status,
        state,
        lga,
        cast(v2_id as varchar)              as v2_id,
        cast(v3_id as varchar)              as v3_id,
        v2_name,
        v3_name,
        cast(distance_m as double)          as match_distance_m,
        -- No coalesce to empty string: a null name must yield a null
        -- similarity, not a spurious near-zero score that bands as 'divergent'.
        -- jaro_winkler_similarity returns null when either argument is null.
        jaro_winkler_similarity(lower(v2_name), lower(v3_name))
                                            as name_similarity
    from matched

    union all

    select
        cast(v2_id as varchar)              as facility_id,
        'dropped'                           as lineage_status,
        state,
        lga,
        cast(v2_id as varchar)              as v2_id,
        cast(null as varchar)               as v3_id,
        v2_name,
        cast(null as varchar)               as v3_name,
        cast(null as double)                as match_distance_m,
        cast(null as double)                as name_similarity
    from dropped

    union all

    select
        cast(v3_id as varchar)              as facility_id,
        'appeared'                          as lineage_status,
        state,
        lga,
        cast(null as varchar)               as v2_id,
        cast(v3_id as varchar)              as v3_id,
        cast(null as varchar)               as v2_name,
        v3_name,
        cast(null as double)                as match_distance_m,
        cast(null as double)                as name_similarity
    from appeared
)

select
    facility_id,
    lineage_status,
    state,
    lga,
    v2_id,
    v3_id,
    v2_name,
    v3_name,
    match_distance_m,
    name_similarity,
    case
        when name_similarity is null        then cast(null as varchar)
        when name_similarity >= 0.92        then 'exact-or-near'
        when name_similarity >= 0.80        then 'close'
        else                                     'divergent'
    end                                     as name_match_band
from lineage
