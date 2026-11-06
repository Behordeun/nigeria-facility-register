-- Per-LGA, per-disease ESPEN endemicity with an ordinal rank.
-- One row per (LGA, disease) for the five ESPEN diseases. The rank comes from
-- the latest known label (the latest label that is not an "Unknown ..."
-- status), so a recent unknown does not erase an earlier measured status, and
-- an LGA never reported with a known status keeps a null rank: unknown is not
-- zero. state_key and lga_key are carried so the marts join on the same
-- normalised keys as the frame.

with espen as (
    select *
    from {{ source('raw', 'ntd_espen_lga') }}
),

ranks as (
    select disease_code, endemicity, endemicity_rank
    from {{ ref('ntd_endemicity_rank') }}
),

-- The basis is a property of the disease, so it is filled even where the
-- LGA's status is unknown and the rank is null.
basis as (
    select distinct disease_code, rank_basis
    from {{ ref('ntd_endemicity_rank') }}
)

select
    e.state,
    e.canonical_lga,
    {{ norm_state('e.state') }}             as state_key,
    {{ norm_lga_key('e.canonical_lga') }}   as lga_key,
    cast(e.espen_iu_id as bigint)           as espen_iu_id,
    e.disease,
    e.disease_code,
    e.endemicity_latest_known,
    cast(e.year_latest_known as integer)    as year_latest_known,
    r.endemicity_rank,
    b.rank_basis,
    e.endemicity_latest,
    cast(e.year_latest as integer)          as year_latest,
    cast(e.years_reported as integer)       as years_reported
from espen e
left join ranks r
  on e.disease_code = r.disease_code
 and e.endemicity_latest_known = r.endemicity
left join basis b
  on e.disease_code = b.disease_code
