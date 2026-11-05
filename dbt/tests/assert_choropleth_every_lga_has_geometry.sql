-- The choropleth is only useful if every LGA carries a polygon: a null geometry
-- is a hole in the map. The not_null contract rejects a null, but this also
-- guards the join itself by failing if the row count is not the full 774 or if
-- any geometry is missing, so a geometry/frame/coverage key mismatch that
-- dropped or un-joined an LGA is caught rather than shipping a partial map.
-- Returns a row only on failure.
with counts as (
    select
        count(*)                                    as n_rows,
        count(geometry_geojson)                     as n_with_geometry
    from {{ ref('lga_coverage_choropleth') }}
)
select n_rows, n_with_geometry
from counts
where n_rows <> 774 or n_with_geometry <> 774
