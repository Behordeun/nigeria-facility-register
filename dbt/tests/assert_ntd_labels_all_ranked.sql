-- Every known ESPEN endemicity label must have a rank in the
-- ntd_endemicity_rank seed. An unmapped label would otherwise leave a null
-- rank, which downstream reads as "status unknown": a measured status silently
-- turned into missing. A new ESPEN label fails the build here until it is
-- ranked in the seed. Also guards the seed itself: one rank per
-- (disease_code, endemicity), so the rank join can never fan rows out.
-- Returns one row per violation.
with known as (
    select distinct disease_code, endemicity_latest_known as endemicity
    from {{ source('raw', 'ntd_espen_lga') }}
    where endemicity_latest_known is not null
)

select k.disease_code, k.endemicity, 'known label has no rank in the seed' as problem
from known k
where not exists (
    select 1 from {{ ref('ntd_endemicity_rank') }} r
    where r.disease_code = k.disease_code and r.endemicity = k.endemicity
)

union all

select disease_code, endemicity, 'seed ranks this label more than once'
from {{ ref('ntd_endemicity_rank') }}
group by disease_code, endemicity
having count(*) > 1
