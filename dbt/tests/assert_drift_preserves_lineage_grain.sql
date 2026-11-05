-- The mart attaches the frame to the lineage through a left join, which must
-- neither drop a lineage record (an inner join by mistake) nor duplicate one (a
-- frame that is not unique on the join key). Either fault would silently change
-- the headline counts. This fails unless the mart has exactly one row per
-- staging lineage record and the same facility_id set. Returns a row only on
-- failure.
with stg as (
    select count(*) as n, count(distinct facility_id) as n_ids
    from {{ ref('stg_version_lineage') }}
),
mart as (
    select count(*) as n, count(distinct facility_id) as n_ids
    from {{ ref('lga_version_drift') }}
)
select stg.n as stg_rows, mart.n as mart_rows, mart.n_ids as mart_ids
from stg, mart
where mart.n <> stg.n or mart.n_ids <> stg.n_ids
