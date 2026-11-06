-- stg_ntd_endemicity_lga carries exactly one row per (LGA, disease): 774 LGAs x
-- 5 diseases, every frame LGA present for every disease. The rank is null
-- exactly when the latest known label is null, so an unknown status is never
-- shown as a rank (or a known one dropped to null).
-- Returns one row per violation.
with stg as (
    select * from {{ ref('stg_ntd_endemicity_lga') }}
),

frame as (
    select
        {{ norm_state('state') }}             as state_key,
        {{ norm_lga_key('canonical_lga') }}   as lga_key
    from {{ source('raw', 'frame_master_index') }}
),

diseases as (
    select distinct disease_code from {{ ref('ntd_endemicity_rank') }}
)

select state_key, lga_key, disease_code, 'duplicate (LGA, disease)' as problem
from stg
group by state_key, lga_key, disease_code
having count(*) > 1

union all

select f.state_key, f.lga_key, d.disease_code, 'frame LGA missing for disease'
from frame f
cross join diseases d
where not exists (
    select 1 from stg s
    where s.state_key = f.state_key and s.lga_key = f.lga_key
      and s.disease_code = d.disease_code
)

union all

select state_key, lga_key, disease_code, 'rank null-ness disagrees with label'
from stg
where (endemicity_rank is null) <> (endemicity_latest_known is null)
