-- Each lineage status fixes which fields may be present, and column-domain
-- checks alone cannot enforce that shape. This fails any row whose fields
-- contradict its status, so a carried-over record can never lack a match and a
-- dropped or appeared record can never carry one:
--   carried-over : both ids present, distance and similarity present
--   dropped      : v2 id only, v3 id / distance / similarity all null
--   appeared     : v3 id only, v2 id / distance / similarity all null
-- Returns a row only on failure.
select
    facility_id,
    lineage_status
from {{ ref('stg_version_lineage') }}
where
    (lineage_status = 'carried-over'
        and (v2_id is null or v3_id is null
             or match_distance_m is null or name_similarity is null))
    or
    (lineage_status = 'dropped'
        and (v2_id is null or v3_id is not null
             or match_distance_m is not null or name_similarity is not null))
    or
    (lineage_status = 'appeared'
        and (v3_id is null or v2_id is not null
             or match_distance_m is not null or name_similarity is not null))
