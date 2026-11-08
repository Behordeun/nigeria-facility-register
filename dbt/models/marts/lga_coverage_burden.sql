-- Mart: one row per LGA with facility coverage, NTD burden, access, the priority flag and the priority score.
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
-- Per-capita coverage: population (WorldPop, the year stg_access_population_lga
-- selects) and births (WorldPop 2015)
-- give facilities per 10,000 people and per 10,000 births, so a large LGA with
-- many facilities is not mistaken for a well-served one.
--
-- Facility mix: counts by level (primary, secondary, tertiary, unknown) and by
-- functional status. GRID3 records functional status only in v3.0, and even
-- there many facilities are "Unknown", so the non-functional share is taken
-- over facilities whose status is known and is null where none is, never 0.
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
        count(*)                   as facilities,
        count(*) filter (where facility_level = 'Primary')   as facilities_primary,
        count(*) filter (where facility_level = 'Secondary') as facilities_secondary,
        count(*) filter (where facility_level = 'Tertiary')  as facilities_tertiary,
        -- Partially functional facilities are open and serving, so they count
        -- as functional; a closed facility counts as not functional.
        count(*) filter (where functional in ('Functional', 'Partially Functional')) as facilities_functional,
        count(*) filter (where functional in ('Not-Functional', 'Closed'))           as facilities_not_functional
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
    from {{ ref('stg_access_population_lga') }}
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

-- Malaria burden (Malaria Atlas Project, P. falciparum, one year): the
-- population-weighted parasite rate in children aged 2-10 and clinical cases per
-- 1,000 people a year. Model estimates; null means not estimated, never 0.
malaria as (
    select
        shapeID as shape_id,
        malaria_year,
        pf_parasite_rate,
        pf_incidence_per_1000
    from {{ source('raw', 'malaria_lga') }}
),

joined as (
    select
        frame.canonical_lga,
        frame.state,
        frame.espen_iu_id,
        coalesce(fac.facilities, 0)        as facilities,
        fac.facility_source_version,
        coalesce(fac.facility_status, 'unmatched') as facility_status,
        coalesce(fac.facilities_primary, 0)    as facilities_primary,
        coalesce(fac.facilities_secondary, 0)  as facilities_secondary,
        coalesce(fac.facilities_tertiary, 0)   as facilities_tertiary,
        -- Blank, "Unknown" or missing level: counted, never guessed.
        coalesce(fac.facilities - fac.facilities_primary - fac.facilities_secondary
                 - fac.facilities_tertiary, 0) as facilities_level_unknown,
        coalesce(fac.facilities_functional, 0)     as facilities_functional,
        coalesce(fac.facilities_not_functional, 0) as facilities_not_functional,
        coalesce(fac.facilities - fac.facilities_functional
                 - fac.facilities_not_functional, 0) as facilities_status_unknown,
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
        mal.malaria_year,
        mal.pf_parasite_rate,
        mal.pf_incidence_per_1000,
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
                                           as facilities_per_10k_births,
        -- Same null rule as the other rates: no matched facility, no rate.
        case when fac.facilities is null then null
             else round(cast(fac.facilities_secondary + fac.facilities_tertiary as double)
                        * 100000 / pop.population, 3) end
                                           as secondary_tertiary_per_100k_population,
        -- Share of the LGA's facilities whose functional status is recorded;
        -- 0 in every v2.0 state, where GRID3 does not record it.
        case when fac.facilities is null then null
             else round(cast(fac.facilities_functional + fac.facilities_not_functional as double)
                        / fac.facilities, 4) end
                                           as functional_status_known_share,
        -- Not functional over the facilities whose status is known; null where
        -- none is known, so an unrecorded LGA is never shown as fully functional.
        case when coalesce(fac.facilities_functional + fac.facilities_not_functional, 0) = 0 then null
             else round(cast(fac.facilities_not_functional as double)
                        / (fac.facilities_functional + fac.facilities_not_functional), 4) end
                                           as not_functional_share
    from frame
    left join fac  on frame.state_key = fac.state_key and frame.lga_key = fac.lga_key
    left join ntd  on frame.state_key = ntd.state_key
    left join ntd_lga
      on frame.state_key = ntd_lga.state_key and frame.lga_key = ntd_lga.lga_key
    left join population pop on frame.shape_id = pop.shape_id
    left join births b       on frame.shape_id = b.shape_id
    left join malaria mal    on frame.shape_id = mal.shape_id
    left join flood fl
      on frame.state_key = fl.state_key and frame.lga_key = fl.lga_key
),

cut as (
    -- thin-coverage threshold: bottom tercile of facility counts among LGAs
    -- that actually matched a facility (facilities > 0).
    select quantile_cont(facilities, 0.33) as thin_cut
    from joined
    where facilities > 0
),

rate_quartiles as (
    -- National quartiles of facilities per 10,000 people over the LGAs that
    -- matched a facility (the rate is null elsewhere).
    select
        quantile_cont(facilities_per_10k_population, 0.25) as q1,
        quantile_cont(facilities_per_10k_population, 0.50) as q2,
        quantile_cont(facilities_per_10k_population, 0.75) as q3
    from joined
    where facilities_per_10k_population is not null
),

malaria_quartiles as (
    -- National quartiles of the malaria parasite rate over the LGAs with an
    -- estimate.
    select
        quantile_cont(pf_parasite_rate, 0.25) as q1,
        quantile_cont(pf_parasite_rate, 0.50) as q2,
        quantile_cont(pf_parasite_rate, 0.75) as q3
    from joined
    where pf_parasite_rate is not null
),

flagged as (
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
            as access_risk_score,
        -- How many of the five diseases have a known status, so a burden score
        -- built from fewer than five can be read as such.
        cast(
            (j.sth_endemicity_rank_lga   is not null)::integer
          + (j.sch_endemicity_rank_lga   is not null)::integer
          + (j.lf_endemicity_rank_lga    is not null)::integer
          + (j.oncho_endemicity_rank_lga is not null)::integer
          + (j.loa_endemicity_rank_lga   is not null)::integer
        as integer) as ntd_diseases_known,
        -- Burden points: the highest known endemicity rank across the five
        -- diseases (greatest ignores nulls), so one severe burden is not
        -- outranked by several mild ones. Null only when no status is known.
        greatest(
            j.sth_endemicity_rank_lga, j.sch_endemicity_rank_lga, j.lf_endemicity_rank_lga,
            j.oncho_endemicity_rank_lga, j.loa_endemicity_rank_lga
        ) as priority_burden_points,
        -- Coverage points: 3 in the lowest national quartile of facilities per
        -- 10,000 people, down to 0 in the highest. A value on a quartile
        -- boundary takes the less urgent band. Null where the rate is null (no
        -- facility matched), never scored as thin coverage.
        case
            when j.facilities_per_10k_population is null then null
            when j.facilities_per_10k_population < q.q1 then 3
            when j.facilities_per_10k_population < q.q2 then 2
            when j.facilities_per_10k_population < q.q3 then 1
            else 0
        end as priority_coverage_points,
        -- Malaria points: 3 in the highest national quartile of the parasite
        -- rate, down to 0 in the lowest, the mirror of coverage points. Relative,
        -- because nearly every LGA sits in the same absolute prevalence band
        -- (10-35%), so fixed bands would not separate LGAs. A value on a
        -- quartile boundary takes the more urgent band. Null where not estimated.
        case
            when j.pf_parasite_rate is null then null
            when j.pf_parasite_rate >= mq.q3 then 3
            when j.pf_parasite_rate >= mq.q2 then 2
            when j.pf_parasite_rate >= mq.q1 then 1
            else 0
        end as priority_malaria_points
    from joined j
    cross join rate_quartiles q
    cross join malaria_quartiles mq
)

-- priority_score (0-11) adds burden (0-3), coverage (0-3), access (0-2) and
-- malaria (0-3) points; any unscored part leaves the score null rather than low.
-- priority_tier reads the score on fixed cut-offs: high 8-11 (about the top
-- tenth on the current data), medium 6-7 (from just above half the range, as
-- before), low 0-5. It sits beside the original priority flag, which is
-- unchanged.
, summed as (
select
    f.*,
    -- "LGA, State": a unique, human-readable key for picking one LGA (six LGA
    -- names recur across states, so the bare name is not enough).
    f.canonical_lga || ', ' || f.state as lga_label,
    f.priority_burden_points + f.priority_coverage_points + f.access_risk_score + f.priority_malaria_points
        as priority_score
from flagged f
),

-- The tier reads priority_score itself, so the sum is written once and the two
-- can never disagree.
scored as (
select
    u.*,
    case
        when u.priority_score is null then null
        when u.priority_score >= 8 then 'high'
        when u.priority_score >= 6 then 'medium'
        else 'low'
    end as priority_tier
from summed u
)

-- Readable text built from the columns above, so every consumer (the
-- dashboard's ranked list, its map hover and anyone querying the mart) reads
-- the same wording and the tests here check it. Missing values are written
-- out ("n/a", "unknown", "not measured"), never shown as 0.
select
    s.*,
    -- Why an LGA scores what it does, one clause per part of the score. Null
    -- exactly when the score is null (a part is not measured).
    case when s.priority_score is not null then
        'burden ' || s.priority_burden_points
        || ' (highest of ' || s.ntd_diseases_known || ' known disease ranks)'
        || ' · coverage ' || s.priority_coverage_points
        || ' (' || printf('%.1f', s.facilities_per_10k_population) || ' facilities per 10,000 people)'
        || ' · access ' || s.access_risk_score
        || case
            when s.hard_to_reach and s.flood_affected_2024 then ' (hard to reach on foot, flooded in 2024)'
            when s.hard_to_reach then ' (hard to reach on foot)'
            when s.flood_affected_2024 then ' (flooded in 2024)'
            else ''
        end
        || ' · malaria ' || s.priority_malaria_points
        || ' (' || printf('%.0f', s.pf_parasite_rate * 100) || '% parasite rate)'
    end as priority_reasons,
    -- A one-line summary of everything the register holds for the LGA, used as
    -- the map hover. Never null.
    s.lga_label
    || ' · ' || format('{:,}', s.facilities)
    || case when s.facilities = 1 then ' facility' else ' facilities' end
    || ' (' || case
        when s.facility_source_version is null then 'no facility matched'
        else 'GRID3 ' || s.facility_source_version || ', ' || coalesce(s.facility_status, 'status n/a')
    end || ')'
    || ' · ' || coalesce(printf('%.1f', s.facilities_per_10k_population) || ' per 10k people', 'per-capita rate n/a')
    || ' · STH ' || coalesce(cast(s.sth_endemicity_rank_lga as varchar), 'unknown')
    || ' · SCH ' || coalesce(cast(s.sch_endemicity_rank_lga as varchar), 'unknown')
    || ' · LF ' || coalesce(cast(s.lf_endemicity_rank_lga as varchar), 'unknown')
    || ' · Oncho ' || coalesce(cast(s.oncho_endemicity_rank_lga as varchar), 'unknown')
    || ' · Loa ' || coalesce(cast(s.loa_endemicity_rank_lga as varchar), 'unknown')
    || ' · ' || coalesce(
        printf('%.0f', s.walking_minutes) || ' min walk, '
        || printf('%.0f', s.walking_over_60min_share * 100) || '% over an hour',
        'travel time n/a')
    || ' · flood 2024: ' || case when s.flood_affected_2024 then 'yes' else 'no' end
    || ' · access risk ' || coalesce(cast(s.access_risk_score as varchar), 'n/a')
    || ' · malaria ' || coalesce(
        printf('%.0f', s.pf_parasite_rate * 100) || '% parasite rate (age 2-10), '
        || printf('%.0f', s.pf_incidence_per_1000) || ' cases per 1,000 a year',
        'n/a')
    || ' · ' || (s.facilities_secondary + s.facilities_tertiary) || ' secondary/tertiary'
    || coalesce(' · ' || printf('%.0f', s.not_functional_share * 100) || '% of known-status facilities not functional', '')
    || ' · priority score ' || coalesce(
        s.priority_score || ' of 11 (burden ' || s.priority_burden_points
        || ' + coverage ' || s.priority_coverage_points
        || ' + access ' || s.access_risk_score
        || ' + malaria ' || s.priority_malaria_points || '), ' || s.priority_tier,
        'n/a')
    || ' · original flag: ' || case when s.priority_high_burden_thin_coverage then 'yes' else 'no' end
    as lga_summary
from scored s
