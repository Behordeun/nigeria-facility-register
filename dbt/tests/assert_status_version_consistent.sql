-- The documented relationship between facility_status and facility_source_version
-- must hold, so independent column-domain checks cannot let a contradictory pair
-- through (a matched status with a null version, or an unmatched status with a
-- version). Returns any row that breaks the contract:
--   current / stale-awaiting-v3  => source_version is v2.0 or v3.0 (not null)
--   unmatched                    => source_version is null
select
    canonical_lga,
    state,
    facility_status,
    facility_source_version
from {{ ref('lga_coverage_burden') }}
where
    (facility_status in ('current', 'stale-awaiting-v3')
        and facility_source_version is null)
    or
    (facility_status = 'unmatched'
        and facility_source_version is not null)
