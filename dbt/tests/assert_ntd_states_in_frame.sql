-- Referential integrity for the NTD burden table: every state it carries must
-- be a real frame state. The coverage mart broadcasts each state's STH burden
-- to that state's LGAs by joining on the state, so a burden row labelled with a
-- state the frame does not know would attach to nothing (silently dropping the
-- burden) or, worse, to the wrong state. This fails the build on any NTD state
-- absent from the frame. State is normalised on both sides so FCT is matched,
-- not reported as a false orphan. Returns a row only on failure.
select distinct
    {{ norm_state('n.state') }} as state
from {{ source('raw', 'ntd_sth_state') }} n
where {{ norm_state('n.state') }} not in (
    select distinct {{ norm_state('f.state') }}
    from {{ source('raw', 'frame_master_index') }} f
)
