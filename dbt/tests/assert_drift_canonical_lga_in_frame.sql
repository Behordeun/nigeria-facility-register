-- Referential integrity for the version-drift mart: every resolved
-- canonical_lga must name a real (state, LGA) pair in the 774-LGA frame. A
-- resolved LGA that is not in the frame means the crosswalk or the frame join
-- produced a key the frame does not contain, which would let a lineage record
-- claim an LGA that does not exist. A null canonical_lga is allowed (an
-- unresolved source label, kept in source_lga for reconciliation) and is not
-- checked here. State is normalised on both sides, so FCT is matched across the
-- frame's 'Federal Capital Territory' and the mart's 'FCT' rather than reported
-- as a false orphan. Returns a row only on failure.
select
    d.facility_id,
    d.state,
    d.canonical_lga
from {{ ref('lga_version_drift') }} d
where d.canonical_lga is not null
  and not exists (
      select 1
      from {{ source('raw', 'frame_master_index') }} f
      where f.canonical_lga = d.canonical_lga
        and {{ norm_state('f.state') }} = {{ norm_state('d.state') }}
  )
