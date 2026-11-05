-- Row-count bounds on the raw inputs: a truncated or empty load (a half-fetched
-- geopackage, a CSV that parsed to a single column, a frame file swapped for the
-- wrong one) should fail the gate, not silently produce a tiny or malformed
-- mart. This asserts the fixed national frame is exactly 774 LGAs, and that the
-- two facility sources and the two reference tables are non-empty. Each source
-- names itself so a failure says which input is wrong. Returns a row per input
-- that is out of bounds, so it returns nothing when every input is sane.
with checks as (
    select 'frame_master_index' as source, count(*) as n, 774 as lo, 774 as hi
    from {{ source('raw', 'frame_master_index') }}
    union all
    select 'grid3_v2', count(*), 1, 2147483647 from {{ source('raw', 'grid3_v2') }}
    union all
    select 'grid3_v3', count(*), 1, 2147483647 from {{ source('raw', 'grid3_v3') }}
    union all
    select 'ntd_sth_state', count(*), 1, 2147483647 from {{ source('raw', 'ntd_sth_state') }}
    union all
    select 'lga_alias_crosswalk', count(*), 1, 2147483647 from {{ source('raw', 'lga_alias_crosswalk') }}
)
select source, n, lo, hi
from checks
where n < lo or n > hi
