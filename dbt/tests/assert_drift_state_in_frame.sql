-- Canonical state labels on the version-drift mart: every state value must be
-- spelled exactly as the frame spells it. The comparison is deliberately raw,
-- not normalised, because the guarantee is one label per state: a lineage
-- spelling such as 'FCT' that only matches the frame's 'Federal Capital
-- Territory' after normalisation would split one state into two values for any
-- filter or group-by on state. A null state is reported too, since it could
-- never match a frame state.
--
-- The anti-join is NOT EXISTS, never NOT IN, so a null on either side cannot
-- make the predicate unknown and let the test pass silently. Returns a row only
-- on failure.
select
    d.state,
    count(*) as n_rows
from {{ ref('lga_version_drift') }} d
where d.state is null
   or not exists (
       select 1
       from {{ source('raw', 'frame_master_index') }} f
       where f.state = d.state
   )
group by d.state
