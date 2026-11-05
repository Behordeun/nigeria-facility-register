-- Row-count bounds on the raw inputs: a truncated, empty or swapped load should
-- fail the gate, not silently produce a mart from incomplete data.
--
-- The frame is the fixed national index, so it is pinned exactly: its DISTINCT
-- normalised (state, canonical_lga) key count must be 774. Counting distinct
-- keys, not rows, means a duplicate-plus-missing pair cannot pass by totalling
-- 774 (assert_frame_keys_unique catches the duplicate independently).
--
-- The facility and reference tables have no committed manifest of expected
-- counts, so their floor depends on the build. On a production refresh
-- (fixture_build false, the default) the floor is the share of each source's
-- known national size below which the load is certainly truncated: GRID3 v2.0
-- is ~51k facilities, v3.0 ~42k, the ESPEN summary one row per state so its
-- floor is the full 37 (a missing state means an incomplete summary), and the
-- crosswalk a small but non-trivial alias set. A partial load that still
-- has rows now fails instead of passing a bare "> 0" check. On the fixture
-- build (fixture_build true) the floor drops to 1, because the committed sample
-- is deliberately a two-state slice. Bounds are var-overridable so an operator
-- can tighten them without editing SQL. Returns a row per input out of bounds.
{% set fixture = var('fixture_build', false) %}
{% set min_v2 = var('min_v2_rows', 1 if fixture else 40000) %}
{% set min_v3 = var('min_v3_rows', 1 if fixture else 30000) %}
{% set min_ntd = var('min_ntd_rows', 1 if fixture else 37) %}
{% set min_xwalk = var('min_crosswalk_rows', 1 if fixture else 1) %}

with distinct_frame_keys as (
    select count(*) as n
    from (
        select distinct {{ norm_state('state') }} as s, canonical_lga
        from {{ source('raw', 'frame_master_index') }}
    )
),

checks as (
    select 'frame_master_index' as source, (select n from distinct_frame_keys) as n,
           774 as lo, 774 as hi
    union all
    select 'grid3_v2', count(*), {{ min_v2 }}, 2147483647 from {{ source('raw', 'grid3_v2') }}
    union all
    select 'grid3_v3', count(*), {{ min_v3 }}, 2147483647 from {{ source('raw', 'grid3_v3') }}
    union all
    select 'ntd_sth_state', count(*), {{ min_ntd }}, 2147483647 from {{ source('raw', 'ntd_sth_state') }}
    union all
    select 'lga_alias_crosswalk', count(*), {{ min_xwalk }}, 2147483647
    from {{ source('raw', 'lga_alias_crosswalk') }}
)

select source, n, lo, hi
from checks
where n < lo or n > hi
