-- register_summary is exactly one row and agrees with the tables it
-- summarises, so the live dashboard text can never drift from the data it
-- describes: the LGA count is the 774 frame, every state is on exactly one
-- version or counted as having no matched facility, the per-version facility counts add up, and facilities placed in
-- LGAs never exceed the authority total, and no count is negative. Returns
-- one row per violation.
with s as (
    select * from {{ ref('register_summary') }}
),

row_count as (
    select count(*) as n from s
)

select 'expected exactly one row' as problem from row_count where n <> 1

union all
select 'lgas is not 774' from s where lgas <> 774

union all
select 'states by version do not add up to states' from s
where states_on_v3 + states_on_v2 + states_without_facilities <> states

union all
select 'per-version facilities do not add up' from s where facilities_v3 + facilities_v2 <> facilities

union all
select 'facilities placed in LGAs exceed the authority total' from s where facilities_in_lgas > facilities

union all
select 'facilities placed in LGAs disagree with the coverage mart' from s
where facilities_in_lgas <> (select coalesce(sum(facilities), 0) from {{ ref('lga_coverage_burden') }})

union all
select 'a known-status count exceeds 774' from s
where greatest(sth_known_lgas, sch_known_lgas, lf_known_lgas, oncho_known_lgas, loa_known_lgas) > 774

union all
select 'a count is negative' from s
where least(states_on_v3, states_on_v2, states_without_facilities, lgas_unmatched, priority_lgas) < 0
