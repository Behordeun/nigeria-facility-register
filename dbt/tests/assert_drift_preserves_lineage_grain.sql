-- The mart attaches the frame to the lineage through a left join, which must
-- neither drop a lineage record (an inner join by mistake) nor duplicate one (a
-- frame that is not unique on the join key). Comparing row counts alone is not
-- enough: a fan-out that duplicates one facility while a different facility is
-- dropped can leave the totals equal while the id set is wrong. This compares
-- the facility_id sets in both directions and fails on any id that is in one
-- table but not the other. Returns a row only on failure.
select facility_id, 'missing_from_mart' as issue
from {{ ref('stg_version_lineage') }}
where facility_id not in (select facility_id from {{ ref('lga_version_drift') }})

union all

select facility_id, 'extra_in_mart' as issue
from {{ ref('lga_version_drift') }}
where facility_id not in (select facility_id from {{ ref('stg_version_lineage') }})
