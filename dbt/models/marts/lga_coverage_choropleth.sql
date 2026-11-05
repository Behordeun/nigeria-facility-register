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
        ST_AsGeoJSON(geom)                  as geometry_geojson
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
    c.espen_iu_id,
    c.facilities,
    c.facility_source_version,
    c.facility_status,
    c.sth_high_share_pct_state,
    c.sth_survey_years_state,
    c.priority_high_burden_thin_coverage,
    b.geometry_geojson
from coverage c
left join frame_keyed fk
    on c.state_key = fk.state_key and c.lga_key = fk.lga_key
left join boundaries b
    on fk.shape_id = b.shape_id
