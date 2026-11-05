-- The match rule is a 100 m ceiling, and it must hold in the built output, not
-- only in the model text: a refactor that widened the radius or broke the
-- pre-filter would otherwise ship silently. This fails any carried-over record
-- whose paired facilities are more than 100 m apart. Returns a row only on
-- failure.
select
    facility_id,
    match_distance_m
from {{ ref('stg_version_lineage') }}
where lineage_status = 'carried-over'
  and match_distance_m > 100
