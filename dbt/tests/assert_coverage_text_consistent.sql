-- The readable text on lga_coverage_burden agrees with the columns it
-- describes: priority_reasons is null exactly when priority_score is null and
-- names each part's points; lga_summary opens with the LGA's label and states
-- its priority score (or n/a). The map hover on lga_coverage_choropleth carries
-- the same text. Returns one row per violation.
with c as (
    select * from {{ ref('lga_coverage_burden') }}
)

select lga_label, 'priority_reasons null-ness differs from priority_score' as problem
from c
where (priority_reasons is null) <> (priority_score is null)

union all

select lga_label, 'priority_reasons does not name each part''s points'
from c
where priority_reasons is not null
  and not (
      starts_with(priority_reasons, 'burden ' || priority_burden_points || ' ')
      and contains(priority_reasons, ' · coverage ' || priority_coverage_points || ' ')
      and contains(priority_reasons, ' · access ' || access_risk_score)
  )

union all

select lga_label, 'lga_summary does not open with the LGA label'
from c
where not starts_with(lga_summary, lga_label || ' · ')

union all

select lga_label, 'lga_summary misstates the priority score'
from c
where not contains(
    lga_summary,
    ' · priority score ' || coalesce(priority_score || ' of 8 ', 'n/a')
)

union all

select m.lga_label, 'map hover differs from lga_coverage_burden'
from {{ ref('lga_coverage_choropleth') }} m
join c on c.lga_label = m.lga_label
where m.lga_summary is distinct from c.lga_summary
