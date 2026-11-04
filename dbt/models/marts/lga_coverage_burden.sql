-- Mart: one row per LGA with facility coverage, NTD burden and the priority flag.
-- Facility counts come from the version-aware authority layer, joined to the
-- canonical 774-LGA frame on a punctuation-normalised LGA name so hyphen and
-- spacing variants (akoko-edo vs akoko edo) do not split a real match.
-- NTD burden is state-level and broadcast to each LGA in the state, labelled so.

with norm as (
    select
        *,
        regexp_replace(lower(regexp_replace(lga, '[/\-\.]', ' ', 'g')), '\s+', ' ', 'g') as lga_key,
        trim(state) as state_key
    from {{ ref('stg_facilities_authority') }}
),

fac as (
    select
        state_key,
        lga_key,
        any_value(source_version)  as facility_source_version,
        any_value(facility_status) as facility_status,
        count(*)                   as facilities
    from norm
    group by state_key, lga_key
),

frame as (
    select
        canonical_lga,
        state,
        espen_iu_id,
        regexp_replace(lower(regexp_replace(canonical_lga, '[/\-\.]', ' ', 'g')), '\s+', ' ', 'g') as lga_key,
        trim(state) as state_key
    from {{ source('raw', 'frame_master_index') }}
),

ntd as (
    select
        trim(state) as state_key,
        sth_high_share_pct,
        sth_survey_years
    from {{ source('raw', 'ntd_sth_state') }}
),

joined as (
    select
        frame.canonical_lga,
        frame.state,
        frame.espen_iu_id,
        coalesce(fac.facilities, 0)        as facilities,
        fac.facility_source_version,
        coalesce(fac.facility_status, 'unmatched') as facility_status,
        ntd.sth_high_share_pct             as sth_high_share_pct_state,
        ntd.sth_survey_years               as sth_survey_years_state
    from frame
    left join fac  on frame.state_key = fac.state_key and frame.lga_key = fac.lga_key
    left join ntd  on frame.state_key = ntd.state_key
),

cut as (
    -- thin-coverage threshold: bottom tercile of facility counts among LGAs
    -- that actually matched a facility (facilities > 0).
    select quantile_cont(facilities, 0.33) as thin_cut
    from joined
    where facilities > 0
)

select
    j.*,
    case
        when j.sth_high_share_pct_state >= 40
         and j.facilities > 0
         and j.facilities <= (select thin_cut from cut)
        then true else false
    end as priority_high_burden_thin_coverage
from joined j
