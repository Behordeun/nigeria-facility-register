-- lga_profile_facts holds exactly facts 1-18 for every LGA in
-- lga_coverage_burden and for no other LGA, so the dashboard's LGA profile
-- always shows the full, ordered set. Its score facts agree with the score
-- columns they describe. Returns one row per violation.
with facts as (
    select * from {{ ref('lga_profile_facts') }}
),

lgas as (
    select * from {{ ref('lga_coverage_burden') }}
),

per_lga as (
    select
        lga_label,
        count(*)                   as n_facts,
        count(distinct fact_order) as n_orders,
        min(fact_order)            as first_order,
        max(fact_order)            as last_order
    from facts
    group by lga_label
)

select l.lga_label, 'missing or incomplete fact set' as problem
from lgas l
left join per_lga p on p.lga_label = l.lga_label
where p.lga_label is null
   or p.n_facts <> 18 or p.n_orders <> 18 or p.first_order <> 1 or p.last_order <> 18

union all

select p.lga_label, 'facts for an LGA not in lga_coverage_burden'
from per_lga p
left join lgas l on l.lga_label = p.lga_label
where l.lga_label is null

union all

select f.lga_label, 'priority score fact disagrees with priority_score and priority_tier'
from facts f
join lgas l on l.lga_label = f.lga_label
where f.fact_order = 2
  and f.value <> coalesce(l.priority_score || ' of 8 (' || l.priority_tier || ')', 'not scored')
