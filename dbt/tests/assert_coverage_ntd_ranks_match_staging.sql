-- Each per-LGA endemicity rank in lga_coverage_burden equals the staging rank
-- for that LGA and disease. Guards the pivot and the frame join: a key miss
-- would show as a null rank in the mart where staging holds a measured one,
-- which reads as "unknown" rather than a join failure.
-- Returns one row per violation.
with stg as (
    select state_key, lga_key, disease_code, endemicity_rank
    from {{ ref('stg_ntd_endemicity_lga') }}
),

mart as (
    select
        {{ norm_state('state') }}             as state_key,
        {{ norm_lga_key('canonical_lga') }}   as lga_key,
        sth_endemicity_rank_lga,
        sch_endemicity_rank_lga,
        lf_endemicity_rank_lga,
        oncho_endemicity_rank_lga,
        loa_endemicity_rank_lga
    from {{ ref('lga_coverage_burden') }}
),

mart_long as (
    select state_key, lga_key, 'STH' as disease_code, sth_endemicity_rank_lga as endemicity_rank from mart
    union all
    select state_key, lga_key, 'SCH', sch_endemicity_rank_lga from mart
    union all
    select state_key, lga_key, 'lf', lf_endemicity_rank_lga from mart
    union all
    select state_key, lga_key, 'Oncho', oncho_endemicity_rank_lga from mart
    union all
    select state_key, lga_key, 'Loa', loa_endemicity_rank_lga from mart
)

select m.state_key, m.lga_key, m.disease_code,
       m.endemicity_rank as mart_rank, s.endemicity_rank as staging_rank
from mart_long m
left join stg s
  on s.state_key = m.state_key and s.lga_key = m.lga_key
 and s.disease_code = m.disease_code
where m.endemicity_rank is distinct from s.endemicity_rank
