-- The priority score on lga_coverage_burden recomputes from its inputs, so every
-- point shown on the dashboard traces back to a rank, a rate or the access
-- score: ntd_diseases_known counts the known ranks, burden points are the
-- highest known rank, coverage points follow the national quartiles of
-- facilities per 10,000 people, access points follow hard_to_reach and the 2024
-- flood flag, malaria points follow the national quartiles of the parasite
-- rate, the score is the sum of its four parts and is
-- null exactly when a part is null, and the tier reads the score on its fixed
-- cut-offs. Returns one row per violation.
with c as (
    select * from {{ ref('lga_coverage_burden') }}
),

q as (
    select
        quantile_cont(facilities_per_10k_population, 0.25) as q1,
        quantile_cont(facilities_per_10k_population, 0.50) as q2,
        quantile_cont(facilities_per_10k_population, 0.75) as q3
    from c
    where facilities_per_10k_population is not null
),

mq as (
    select
        quantile_cont(pf_parasite_rate, 0.25) as q1,
        quantile_cont(pf_parasite_rate, 0.50) as q2,
        quantile_cont(pf_parasite_rate, 0.75) as q3
    from c
    where pf_parasite_rate is not null
),

expected as (
    select
        c.*,
        (case when sth_endemicity_rank_lga   is not null then 1 else 0 end
       + case when sch_endemicity_rank_lga   is not null then 1 else 0 end
       + case when lf_endemicity_rank_lga    is not null then 1 else 0 end
       + case when oncho_endemicity_rank_lga is not null then 1 else 0 end
       + case when loa_endemicity_rank_lga   is not null then 1 else 0 end) as known_expected,
        (select max(r) from (values
            (sth_endemicity_rank_lga), (sch_endemicity_rank_lga), (lf_endemicity_rank_lga),
            (oncho_endemicity_rank_lga), (loa_endemicity_rank_lga)) v(r))  as burden_expected,
        case
            when facilities_per_10k_population is null then null
            when facilities_per_10k_population < q.q1 then 3
            when facilities_per_10k_population < q.q2 then 2
            when facilities_per_10k_population < q.q3 then 1
            else 0
        end                                                                 as coverage_expected,
        -- Recomputed from its inputs rather than read from access_risk_score,
        -- so the score is checked end to end even if that column drifts.
        case
            when hard_to_reach is null then null
            else (case when hard_to_reach then 1 else 0 end)
               + (case when flood_affected_2024 then 1 else 0 end)
        end                                                                 as access_expected,
        case
            when pf_parasite_rate is null then null
            when pf_parasite_rate >= mq.q3 then 3
            when pf_parasite_rate >= mq.q2 then 2
            when pf_parasite_rate >= mq.q1 then 1
            else 0
        end                                                                 as malaria_expected
    from c
    cross join q
    cross join mq
)

select state, canonical_lga, 'ntd_diseases_known does not count the known ranks' as problem
from expected
where ntd_diseases_known is distinct from known_expected

union all
select state, canonical_lga, 'priority_burden_points is not the highest known rank'
from expected
where priority_burden_points is distinct from burden_expected

union all
select state, canonical_lga, 'priority_coverage_points does not follow the rate quartiles'
from expected
where priority_coverage_points is distinct from coverage_expected

union all
select state, canonical_lga, 'priority_malaria_points does not follow the parasite-rate quartiles'
from expected
where priority_malaria_points is distinct from malaria_expected

union all
select state, canonical_lga, 'priority_score is not the sum of its parts (null when any part is null)'
from expected
where priority_score is distinct from (burden_expected + coverage_expected + access_expected + malaria_expected)

union all
select state, canonical_lga, 'priority_tier does not follow the score cut-offs'
from expected
where priority_tier is distinct from (
    case
        when priority_score is null then null
        when priority_score >= 8 then 'high'
        when priority_score >= 6 then 'medium'
        else 'low'
    end
)
