-- The choropleth is only useful if every LGA carries at least one polygon: a
-- missing geometry is a hole in the map. The vendored boundaries are pre-split
-- so a MultiPolygon LGA spans several single-Polygon rows, which means the total
-- row count is no longer 774. This checks the property that still holds: every
-- one of the 774 canonical LGAs appears at least once, and no row carries a null
-- geometry. A geometry, frame, or coverage key mismatch that dropped or un-joined
-- an LGA is caught rather than shipping a partial map. Returns a row only on
-- failure.
with distinct_lgas as (
    -- An LGA is a (state, canonical_lga) pair: canonical_lga is not unique
    -- nationally, since two states can carry an LGA of the same name, so the
    -- grain is the pair, not the name alone.
    select count(distinct (state, canonical_lga)) as n_lgas
    from {{ ref('lga_coverage_choropleth') }}
),

null_geometry as (
    select count(*) as n_null_geometry
    from {{ ref('lga_coverage_choropleth') }}
    where geometry_geojson is null
)

select n_lgas, n_null_geometry
from distinct_lgas, null_geometry
where n_lgas <> 774 or n_null_geometry <> 0
