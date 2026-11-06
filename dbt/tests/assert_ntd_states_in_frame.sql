-- Referential integrity for the NTD burden table: every state it carries must
-- be a real frame state. The coverage mart broadcasts each state's STH burden
-- to that state's LGAs by joining on the state, so a burden row labelled with a
-- state the frame does not know would attach to nothing (silently dropping the
-- burden) or, worse, to the wrong state. This fails the build on any NTD state
-- absent from the frame, and on a null NTD state, which could never join.
--
-- The anti-join is NOT EXISTS, never NOT IN: a null state on either side makes a
-- NOT IN predicate evaluate to unknown for every row, so the test would return
-- nothing and pass while the burden silently failed to join. State is
-- normalised on both sides so FCT is matched, not reported as a false orphan.
-- Returns a row only on failure.
select distinct
    n.state
from {{ source('raw', 'ntd_sth_state') }} n
where n.state is null
   or not exists (
       select 1
       from {{ source('raw', 'frame_master_index') }} f
       where {{ norm_state('f.state') }} = {{ norm_state('n.state') }}
   )
