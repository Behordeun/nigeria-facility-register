-- Choropleth-ready coverage mart: lga_coverage_burden with each LGA's boundary
-- polygon attached, so a BI tool (Superset deck.gl) can draw coverage x burden
-- on a map without a runtime spatial join.
--
-- The geometry comes from the vendored, simplified geoBoundaries polygons
-- (data/frame/nigeria_774_lga_boundaries.simplified.geojson), read with the
-- DuckDB spatial extension (loaded via the profile). The polygons key on
-- shapeID; the frame carries shapeID alongside the (state, canonical_lga) the
-- coverage mart keys on, so geometry joins to coverage through the frame. The
-- geometry is emitted as GeoJSON text (ST_AsGeoJSON), which Superset reads
-- directly; the grain stays one row per LGA, all 774 present.
--
-- geojson_path is a var so the location is not hardcoded in SQL; it defaults to
-- the committed artifact relative to the dbt working directory.

{% set geojson_path = var(
    'lga_geojson_path', '../data/frame/nigeria_774_lga_boundaries.simplified.geojson'
) %}

with boundaries as (
    select
        shapeID                             as shape_id,
        -- Cast to varchar so the column is plain GeoJSON text. A json-typed
        -- column comes back from the driver as a parsed object, which a deck.gl
        -- map layer cannot read: it expects a string it parses itself.
        -- ST_AsGeoJSON already returns text; the cast pins the column type to
        -- varchar so the contract and the driver agree.
        cast(ST_AsGeoJSON(geom) as varchar)  as geometry_geojson
    from st_read('{{ geojson_path }}')
),

frame as (
    select
        shapeID                             as shape_id,
        canonical_lga,
        {{ norm_state('state') }}           as state_key
    from {{ source('raw', 'frame_master_index') }}
),

coverage as (
    select
        *,
        {{ norm_state('state') }}           as state_key,
        {{ norm_lga_key('canonical_lga') }} as lga_key
    from {{ ref('lga_coverage_burden') }}
),

frame_keyed as (
    select
        f.shape_id,
        f.state_key,
        {{ norm_lga_key('f.canonical_lga') }} as lga_key
    from frame f
)

select
    c.canonical_lga,
    c.state,
    c.lga_label,
    c.espen_iu_id,
    c.facilities,
    c.facility_source_version,
    c.facility_status,
    c.sth_high_share_pct_state,
    c.sth_survey_years_state,
    c.priority_high_burden_thin_coverage,
    c.sth_endemicity_rank_lga,
    c.sch_endemicity_rank_lga,
    c.lf_endemicity_rank_lga,
    c.oncho_endemicity_rank_lga,
    c.loa_endemicity_rank_lga,
    c.population,
    c.facilities_per_10k_population,
    c.motorised_minutes,
    c.walking_minutes,
    c.walking_over_60min_share,
    c.flood_affected_2024,
    c.hard_to_reach,
    c.access_risk_score,
    c.facilities_secondary,
    c.facilities_tertiary,
    c.secondary_tertiary_per_100k_population,
    c.functional_status_known_share,
    c.not_functional_share,
    c.ntd_diseases_known,
    c.priority_burden_points,
    c.priority_coverage_points,
    c.priority_score,
    c.priority_tier,
    b.geometry_geojson,
    -- A complete GeoJSON Feature per LGA: the deck.gl Geojson layer reads
    -- .geometry and .properties off each row, so a bare geometry (what
    -- geometry_geojson holds) gives it nothing to key on. The geometry text is
    -- parsed back to a JSON value with json() so it nests as an object, not a
    -- quoted string, and the properties carry the fields a map colours or labels
    -- by. Emitted as varchar for the same reason geometry_geojson is.
    cast(
        json_object(
            'type', 'Feature',
            'geometry', json(b.geometry_geojson),
            'properties', json_object(
                'canonical_lga', c.canonical_lga,
                'state', c.state,
                'facilities', c.facilities,
                'facility_status', c.facility_status,
                'sth_high_share_pct_state', c.sth_high_share_pct_state,
                'priority_high_burden_thin_coverage', c.priority_high_burden_thin_coverage,
                'sth_endemicity_rank_lga', c.sth_endemicity_rank_lga,
                'sch_endemicity_rank_lga', c.sch_endemicity_rank_lga,
                'lf_endemicity_rank_lga', c.lf_endemicity_rank_lga,
                'oncho_endemicity_rank_lga', c.oncho_endemicity_rank_lga,
                'loa_endemicity_rank_lga', c.loa_endemicity_rank_lga,
                'population', c.population,
                'facilities_per_10k_population', c.facilities_per_10k_population,
                'motorised_minutes', c.motorised_minutes,
                'walking_minutes', c.walking_minutes,
                'walking_over_60min_share', c.walking_over_60min_share,
                'flood_affected_2024', c.flood_affected_2024,
                'hard_to_reach', c.hard_to_reach,
                'access_risk_score', c.access_risk_score,
                'secondary_tertiary_per_100k_population', c.secondary_tertiary_per_100k_population,
                'not_functional_share', c.not_functional_share,
                'ntd_diseases_known', c.ntd_diseases_known,
                'priority_burden_points', c.priority_burden_points,
                'priority_coverage_points', c.priority_coverage_points,
                'priority_score', c.priority_score,
                'priority_tier', c.priority_tier
            )
        ) as varchar
    )                                       as geometry_feature
from coverage c
left join frame_keyed fk
    on c.state_key = fk.state_key and c.lga_key = fk.lga_key
left join boundaries b
    on fk.shape_id = b.shape_id
