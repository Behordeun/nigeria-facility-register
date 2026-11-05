-- The mart's grain guarantee: exactly 774 LGAs, one row each. The frame has 774
-- LGAs and the mart is one row per frame LGA, so any other count means a join
-- fanned out or dropped rows. A test returns the offending rows, so it returns
-- a single row carrying the actual count when (and only when) it is not 774.
with n as (
    select count(*) as lga_rows from {{ ref('lga_coverage_burden') }}
)
select lga_rows from n where lga_rows <> 774
