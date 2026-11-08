-- lga_profile_facts: everything the register holds for one LGA, as readable
-- text, one row per LGA and fact.
--
-- The dashboard's LGA profile shows these rows for the chosen LGA in fact_order.
-- Keeping the wording here, not in the dashboard, puts it under the contract and
-- the tests, and lets the dashboard show it as a plain table instead of
-- rendering HTML. Every value is text; a missing input is written out ("n/a",
-- "unknown", "not measured", "not scored"), never shown as 0.
--
-- Grain: one row per (lga_label, fact_order), 20 facts for each of the 774 LGAs.

with lgas as (
    select * from {{ ref('lga_coverage_burden') }}
)

select
    lga_label,
    unnest([
        {'fact_order': 1, 'section': 'LGA', 'fact': 'LGA, State',
         'value': lga_label},

        {'fact_order': 2, 'section': 'Priority', 'fact': 'Priority score',
         'value': coalesce(priority_score || ' of 11 (' || priority_tier || ')', 'not scored')},
        {'fact_order': 3, 'section': 'Priority', 'fact': 'Score parts',
         'value': 'burden ' || coalesce(cast(priority_burden_points as varchar), 'n/a')
             || ' + coverage ' || coalesce(cast(priority_coverage_points as varchar), 'n/a')
             || ' + access ' || coalesce(cast(access_risk_score as varchar), 'n/a')
             || ' + malaria ' || coalesce(cast(priority_malaria_points as varchar), 'n/a')},
        {'fact_order': 4, 'section': 'Priority', 'fact': 'Original flag (high STH share, thin coverage)',
         'value': case when priority_high_burden_thin_coverage then 'yes' else 'no' end},

        {'fact_order': 5, 'section': 'Facilities', 'fact': 'In the register',
         'value': format('{:,}', facilities) || ' (' || case
             when facility_source_version is null then 'no facility matched'
             else 'GRID3 ' || facility_source_version || ', ' || coalesce(facility_status, 'status n/a')
         end || ')'},
        {'fact_order': 6, 'section': 'Facilities', 'fact': 'By level',
         'value': facilities_primary || ' primary, ' || facilities_secondary || ' secondary, '
             || facilities_tertiary || ' tertiary, ' || facilities_level_unknown || ' not recorded'},
        {'fact_order': 7, 'section': 'Facilities', 'fact': 'Per 10,000 people',
         'value': coalesce(printf('%.2f', facilities_per_10k_population), 'n/a')},
        {'fact_order': 8, 'section': 'Facilities', 'fact': 'Secondary and tertiary per 100,000 people',
         'value': coalesce(printf('%.2f', secondary_tertiary_per_100k_population), 'n/a')},
        {'fact_order': 9, 'section': 'Facilities', 'fact': 'Not functional (of known status)',
         'value': coalesce(
             printf('%.0f', not_functional_share * 100) || '% of '
             || (facilities_functional + facilities_not_functional) || ' with a recorded status',
             'status not recorded')},

        {'fact_order': 10, 'section': 'People and access', 'fact': 'Population (WorldPop ' || population_year || ')',
         'value': coalesce(format('{:,}', population), 'n/a')},
        {'fact_order': 11, 'section': 'People and access', 'fact': 'Walking time to a facility',
         'value': coalesce(
             printf('%.0f', walking_minutes) || ' min; '
             || printf('%.0f', walking_over_60min_share * 100) || '% over an hour',
             'not measured')},
        {'fact_order': 12, 'section': 'People and access', 'fact': 'Motorised time',
         'value': coalesce(printf('%.0f', motorised_minutes) || ' min', 'not measured')},
        {'fact_order': 13, 'section': 'People and access', 'fact': 'Flood-affected in 2024',
         'value': case when flood_affected_2024 then 'yes' else 'no' end},

        {'fact_order': 14, 'section': 'NTD endemicity rank (0-3)', 'fact': 'Soil-transmitted helminths',
         'value': coalesce(cast(sth_endemicity_rank_lga as varchar), 'unknown')},
        {'fact_order': 15, 'section': 'NTD endemicity rank (0-3)', 'fact': 'Schistosomiasis',
         'value': coalesce(cast(sch_endemicity_rank_lga as varchar), 'unknown')},
        {'fact_order': 16, 'section': 'NTD endemicity rank (0-3)', 'fact': 'Lymphatic filariasis',
         'value': coalesce(cast(lf_endemicity_rank_lga as varchar), 'unknown')},
        {'fact_order': 17, 'section': 'NTD endemicity rank (0-3)', 'fact': 'Onchocerciasis',
         'value': coalesce(cast(oncho_endemicity_rank_lga as varchar), 'unknown')},
        {'fact_order': 18, 'section': 'NTD endemicity rank (0-3)', 'fact': 'Loa loa',
         'value': coalesce(cast(loa_endemicity_rank_lga as varchar), 'unknown')},

        {'fact_order': 19, 'section': 'Malaria (MAP ' || malaria_year || ')', 'fact': 'Parasite rate, age 2-10',
         'value': coalesce(printf('%.0f', pf_parasite_rate * 100) || '%', 'not estimated')},
        {'fact_order': 20, 'section': 'Malaria (MAP ' || malaria_year || ')', 'fact': 'Clinical cases per 1,000 a year',
         'value': coalesce(printf('%.0f', pf_incidence_per_1000), 'not estimated')}
    ], recursive := true)
from lgas
