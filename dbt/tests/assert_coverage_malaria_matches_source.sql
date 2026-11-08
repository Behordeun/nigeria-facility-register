-- The malaria columns on lga_coverage_burden equal the per-LGA malaria table for
-- every LGA, through the frame's shapeID, and describe one malaria year. A
-- parasite rate stays within 0-1 and incidence is never negative. Returns one
-- row per violation.
with c as (
    select * from {{ ref('lga_coverage_burden') }}
),

frame as (
    select shapeID, canonical_lga, state
    from {{ source('raw', 'frame_master_index') }}
),

src as (
    select * from {{ source('raw', 'malaria_lga') }}
)

select c.lga_label, 'malaria columns differ from the source table' as problem
from c
join frame f on f.canonical_lga = c.canonical_lga and f.state = c.state
left join src s on s.shapeID = f.shapeID
where c.malaria_year is distinct from s.malaria_year
   or c.pf_parasite_rate is distinct from s.pf_parasite_rate
   or c.pf_incidence_per_1000 is distinct from s.pf_incidence_per_1000

union all

select lga_label, 'parasite rate outside 0-1 or negative incidence'
from c
where pf_parasite_rate not between 0 and 1 or pf_incidence_per_1000 < 0

union all

select 'all', 'more than one malaria year'
from c
having count(distinct malaria_year) > 1
