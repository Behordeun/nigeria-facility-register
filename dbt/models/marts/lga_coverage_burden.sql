-- Mart: one row per LGA with facility coverage, NTD burden and the priority flag.
-- Facility counts come from the version-aware authority layer, joined to the
-- canonical 774-LGA frame. The join keys on (canonical state, normalised LGA).
--
-- State is canonicalised with norm_state so a v2/v3 alias (FCT, Abuja vs FCT)
-- does not leave a covered state unmatched. LGA names are punctuation-normalised
-- (akoko-edo vs akoko edo), and a committed alias crosswalk first maps
-- facility-source LGA spellings and former names to the canonical frame name
-- (Municipal Area Council -> AMAC, Shomolu -> Somolu), so a known variant joins
-- instead of showing a false zero.
--
-- Burden comes at two resolutions. sth_high_share_pct_state is a state figure
-- broadcast to every LGA in the state and drives the priority flag, unchanged.
-- The *_endemicity_rank_lga columns are true per-LGA ESPEN endemicity ranks
-- (0-3, null when the LGA's status was never known) for five diseases, pivoted
-- from stg_ntd_endemicity_lga. They sit alongside the state column rather than
-- replacing it, so the existing column keeps its meaning.

with xwalk_raw as (
    select state, facility_lga, canonical_lga
    from {{ source('raw', 'lga_alias_crosswalk') }}
),

xwalk as (
    select
        {{ norm_state('state') }} as state_key,
        facility_lga,
        canonical_lga
    from xwalk_raw
),

authority_keyed as (
    select
        *,
        {{ norm_state('state') }} as state_key
    from {{ ref('stg_facilities_authority') }}
),

authority as (
    select
        a.*,
        coalesce(x.canonical_lga, a.lga) as resolved_lga
    from authority_keyed a
    left join xwalk x
      on a.state_key = x.state_key and a.lga = x.facility_lga
),

norm as (
    select
        *,
        {{ norm_lga_key('resolved_lga') }} as lga_key
    from authority
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
        {{ norm_lga_key('canonical_lga') }} as lga_key,
        {{ norm_state('state') }} as state_key
    from {{ source('raw', 'frame_master_index') }}
),

ntd as (
    select
        {{ norm_state('state') }} as state_key,
        sth_high_share_pct,
        sth_survey_years
    from {{ source('raw', 'ntd_sth_state') }}
),

ntd_lga as (
    select
        state_key,
        lga_key,
        max(endemicity_rank) filter (where disease_code = 'STH')   as sth_endemicity_rank_lga,
        max(endemicity_rank) filter (where disease_code = 'SCH')   as sch_endemicity_rank_lga,
        max(endemicity_rank) filter (where disease_code = 'lf')    as lf_endemicity_rank_lga,
        max(endemicity_rank) filter (where disease_code = 'Oncho') as oncho_endemicity_rank_lga,
        max(endemicity_rank) filter (where disease_code = 'Loa')   as loa_endemicity_rank_lga
    from {{ ref('stg_ntd_endemicity_lga') }}
    group by state_key, lga_key
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
        ntd.sth_survey_years               as sth_survey_years_state,
        ntd_lga.sth_endemicity_rank_lga,
        ntd_lga.sch_endemicity_rank_lga,
        ntd_lga.lf_endemicity_rank_lga,
        ntd_lga.oncho_endemicity_rank_lga,
        ntd_lga.loa_endemicity_rank_lga
    from frame
    left join fac  on frame.state_key = fac.state_key and frame.lga_key = fac.lga_key
    left join ntd  on frame.state_key = ntd.state_key
    left join ntd_lga
      on frame.state_key = ntd_lga.state_key and frame.lga_key = ntd_lga.lga_key
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
