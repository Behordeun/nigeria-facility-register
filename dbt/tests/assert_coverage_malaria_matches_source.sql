-- Every frame LGA has a row in the per-LGA malaria table, and the malaria
-- columns on lga_coverage_burden equal it, through the frame's shapeID, for one
-- malaria year. A parasite rate stays within 0-1, incidence is never negative,
-- and the two are missing for the same LGAs. Returns one row per violation.
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

-- A missing source row would make both sides null below, which compares equal,
-- so it is checked on its own.
select c.lga_label, 'LGA missing from the malaria table' as problem
from c
join frame f on f.canonical_lga = c.canonical_lga and f.state = c.state
left join src s on s.shapeID = f.shapeID
where s.shapeID is null

union all

select c.lga_label, 'malaria columns differ from the source table'
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

select lga_label, 'parasite rate and incidence are not missing together'
from c
where (pf_parasite_rate is null) <> (pf_incidence_per_1000 is null)

union all

select 'all', 'more than one malaria year'
from c
having count(distinct malaria_year) > 1
