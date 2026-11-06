-- Mart: the v2-to-v3 facility lineage, made queryable against the canonical
-- 774-LGA frame. One row per facility-lineage record (carried-over / dropped /
-- appeared), the same grain as stg_version_lineage, with the canonical LGA and
-- state resolved so the lineage answers "what changed in this LGA between
-- versions" without the caller re-doing the name reconciliation.
--
-- The source LGA label is resolved to the canonical frame name through the same
-- alias crosswalk the coverage mart uses (Municipal Area Council -> AMAC), then
-- punctuation-normalised and joined to the frame on (canonical state,
-- normalised LGA). canonical_lga is null when the source LGA does not resolve to
-- a frame LGA: that is an unresolved label, read it as such, never as "no LGA".
-- The unresolved source label is kept as source_lga so a null canonical_lga is
-- still actionable for reconciliation instead of opaque.
--
-- The frame is reduced to one row per (state, LGA) key before the join, so a
-- duplicate frame row can never fan a single lineage record into many and break
-- the one-row-per-facility grain. The join is a left join, so a lineage record
-- whose LGA does not resolve is kept with a null canonical_lga, never dropped.
--
-- state is always the frame's state name, resolved from the lineage state's
-- normalised key rather than from the LGA join, so a record whose LGA does not
-- resolve still carries the same label as its resolved neighbours (the frame's
-- "Federal Capital Territory", never the lineage "FCT"). One label per state is
-- what lets a filter or a group-by on state treat each state as one value.
--
-- Only states v3.0 covers carry lineage; stg_version_lineage already excludes
-- the rest, so every row here is a real cross-version comparison.

with lineage as (
    select
        *,
        {{ norm_state('state') }} as state_key
    from {{ ref('stg_version_lineage') }}
),

xwalk as (
    select
        {{ norm_state('state') }} as state_key,
        facility_lga,
        canonical_lga
    from {{ source('raw', 'lga_alias_crosswalk') }}
),

resolved as (
    select
        l.*,
        coalesce(x.canonical_lga, l.lga) as resolved_lga
    from lineage l
    left join xwalk x
      on l.state_key = x.state_key and l.lga = x.facility_lga
),

normed as (
    select
        *,
        {{ norm_lga_key('resolved_lga') }} as lga_key
    from resolved
),

frame as (
    -- One row per (state, LGA) key. The 774-LGA frame is unique on this key (and
    -- a dedicated test asserts it), so this qualify only guards the join against
    -- a future duplicate; it never silently collapses distinct LGAs today.
    select
        canonical_lga,
        state,
        espen_iu_id,
        lga_key,
        state_key
    from (
        select
            canonical_lga,
            state,
            espen_iu_id,
            {{ norm_lga_key('canonical_lga') }} as lga_key,
            {{ norm_state('state') }} as state_key,
            row_number() over (
                partition by {{ norm_state('state') }}, {{ norm_lga_key('canonical_lga') }}
                order by canonical_lga
            ) as rn
        from {{ source('raw', 'frame_master_index') }}
    )
    where rn = 1
),

frame_state as (
    -- One frame state name per normalised state key. min() only makes the pick
    -- deterministic; the frame carries a single spelling per state.
    select
        {{ norm_state('state') }} as state_key,
        min(state) as state
    from {{ source('raw', 'frame_master_index') }}
    group by 1
)

select
    n.facility_id,
    n.lineage_status,
    f.canonical_lga,
    n.lga                               as source_lga,
    -- Falls back to the lineage label only for a state the frame does not know,
    -- so the row still materialises and assert_drift_state_in_frame names it.
    coalesce(fs.state, n.state)         as state,
    f.espen_iu_id,
    n.v2_id,
    n.v3_id,
    n.v2_name,
    n.v3_name,
    n.match_distance_m,
    n.name_similarity,
    n.name_match_band
from normed n
left join frame f
  on n.state_key = f.state_key and n.lga_key = f.lga_key
left join frame_state fs
  on n.state_key = fs.state_key
