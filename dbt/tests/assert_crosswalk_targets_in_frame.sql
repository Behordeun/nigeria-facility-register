-- Referential integrity for the alias crosswalk: every canonical_lga it maps a
-- source LGA to must be a real (state, LGA) pair in the frame. The crosswalk is
-- the one place a human types a canonical name by hand, so a typo there would
-- silently route a facility to a frame LGA that does not exist and resolve it
-- against nothing. This fails the build on any crosswalk target the frame does
-- not contain. State is normalised on both sides so FCT is not a false orphan.
-- Returns a row only on failure.
select
    x.state,
    x.facility_lga,
    x.canonical_lga
from {{ source('raw', 'lga_alias_crosswalk') }} x
where not exists (
    select 1
    from {{ source('raw', 'frame_master_index') }} f
    where f.canonical_lga = x.canonical_lga
      and {{ norm_state('f.state') }} = {{ norm_state('x.state') }}
)
