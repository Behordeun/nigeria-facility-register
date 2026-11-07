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
--
-- Per-capita coverage: population (WorldPop 2025) and births (WorldPop 2015)
-- give facilities per 10,000 people and per 10,000 births, so a large LGA with
-- many facilities is not mistaken for a well-served one.
--
-- Access and flood exposure: population-weighted travel time to the nearest
-- health facility (Malaria Atlas Project, from the same per-LGA table as the
-- population) and the flood-event flags. The 2022 flood flag is state-level
-- (the state's flag repeated to each LGA) and the 2024 flag LGA-level, so the
-- column names say which. access_risk_score combines only LGA-resolution
-- signals: being hard to reach on foot and the 2024 flood.

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
        shapeID                             as shape_id,
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

-- Denominators, keyed on the frame's geoBoundaries shapeID. Both inputs cover
-- all 774 LGAs with a positive figure (tested at the source), so a rate is null
-- only where the facility count itself is not a measurement (see below).
population as (
    select
        shapeID as shape_id,
        population,
        population_year,
        motorised_minutes,
        walking_minutes,
        walking_over_60min_share,
        access_population_share
    from {{ source('raw', 'access_population_lga') }}
),

-- One row per LGA, both events pivoted. The source covers all 774 LGAs for
-- each event (tested at the source), so a flag is never missing for a frame LGA.
flood as (
    select
        {{ norm_state('state') }}            as state_key,
        {{ norm_lga_key('canonical_lga') }}  as lga_key,
        bool_or(affected = 1) filter (where event_year = 2022) as flood_affected_2022_state,
        bool_or(affected = 1) filter (where event_year = 2024) as flood_affected_2024
    from {{ source('raw', 'flood_event_affected_lga') }}
    group by all
),

births as (
    select shapeID as shape_id, births
    from {{ source('raw', 'births_lga') }}
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
        ntd_lga.loa_endemicity_rank_lga,
        pop.population,
        pop.population_year,
        b.births,
        pop.motorised_minutes,
        pop.walking_minutes,
        pop.walking_over_60min_share,
        pop.access_population_share,
        fl.flood_affected_2022_state,
        fl.flood_affected_2024,
        -- Hard to reach: at least a quarter of residents live more than an
        -- hour's walk from the nearest facility. Null where the travel figures
        -- are null (no population on valid travel-time cells), never false.
        pop.walking_over_60min_share >= 0.25 as hard_to_reach,
        -- Rates are null, not 0, where no facility matched the LGA: a zero count
        -- there may be a name-join miss, so a rate of 0 would assert an absence
        -- the data has not established (the same reasoning as the priority flag).
        case when fac.facilities is null then null
             else round(cast(fac.facilities as double) * 10000 / pop.population, 3) end
                                           as facilities_per_10k_population,
        case when fac.facilities is null then null
             else round(cast(fac.facilities as double) * 10000 / b.births, 3) end
                                           as facilities_per_10k_births
    from frame
    left join fac  on frame.state_key = fac.state_key and frame.lga_key = fac.lga_key
    left join ntd  on frame.state_key = ntd.state_key
    left join ntd_lga
      on frame.state_key = ntd_lga.state_key and frame.lga_key = ntd_lga.lga_key
    left join population pop on frame.shape_id = pop.shape_id
    left join births b       on frame.shape_id = b.shape_id
    left join flood fl
      on frame.state_key = fl.state_key and frame.lga_key = fl.lga_key
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
    end as priority_high_burden_thin_coverage,
    -- 0-2: one point for being hard to reach on foot, one for the 2024 flood.
    -- Null when hard_to_reach is null, so an unmeasured LGA is not scored as
    -- low risk. The state-level 2022 flag is left out: it marks 727 of 774 LGAs
    -- and so does not separate one LGA from another.
    cast(j.hard_to_reach as integer) + cast(j.flood_affected_2024 as integer)
        as access_risk_score
from joined j
