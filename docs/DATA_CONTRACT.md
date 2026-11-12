# Data contract

This is the promise the published tables make to anyone who reads them. It covers the six serving marts (`lga_coverage_burden`, `lga_coverage_outlook`, `lga_profile_facts`, `lga_version_drift`, `lga_coverage_choropleth` and `register_summary`), the staging layers they are built on, and `register_changes`, which the publish step writes. Column types are enforced by dbt model contracts (`contract: enforced` in the schema files); the guarantees below that a type cannot express are enforced by dbt tests. A change that breaks any guarantee here is a breaking change and follows the versioning rule at the end.

## Scope and grain

| Table | Layer | Grain | Rows |
| --- | --- | --- | --- |
| `stg_facilities_authority` | staging | one facility | one per facility in the authoritative set |
| `stg_version_lineage` | staging | one facility-lineage record | one per v2.0/v3.0 facility in a v3-covered state |
| `stg_ntd_endemicity_lga` | staging | one (LGA, disease) | exactly 774 x 5 = 3,870 |
| `stg_access_population_lga` | staging | one LGA, for one population year | exactly 774 |
| `lga_coverage_burden` | mart | one LGA | exactly 774 |
| `lga_version_drift` | mart | one facility-lineage record | one per `stg_version_lineage` record |
| `lga_coverage_outlook` | mart | one (LGA, population year) | exactly 774 x the WorldPop years carried (2025-2030: 4,644) |
| `lga_profile_facts` | mart | one (LGA, fact) | exactly 774 x 20 = 15,480 |
| `register_summary` | mart | the published register | exactly 1 |
| `register_changes` | publish | one change against the register this one replaced | 1 summary row, plus one per change listed |
| `lga_coverage_choropleth` | mart | one LGA boundary polygon part | one per polygon part (800 on the vendored set) covering exactly 774 LGAs, each with a boundary polygon |

## `stg_facilities_authority`

The version-aware authoritative facility layer: v3.0 where it covers a state, v2.0 everywhere else.

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `facility_id` | varchar | no | unique across the table; `globalid` for v2.0 rows, `unique_id` for v3.0 |
| `state` | varchar | no | a Nigerian state or FCT, normalised |
| `lga` | varchar | no | facility-source LGA name (resolved against the frame in the mart, not here) |
| `facility_name` | varchar | yes | may be absent in the source |
| `facility_level` | varchar | yes | |
| `functional` | varchar | yes | populated only for v3.0 rows; null for v2.0, which has no such field |
| `latitude` | double | yes | null where the source dropped coordinates |
| `longitude` | double | yes | as above |
| `source_version` | varchar | no | one of `v2.0`, `v3.0` |
| `source_date` | date | no | the version's release date |
| `facility_status` | varchar | no | one of `current`, `stale-awaiting-v3` |

Guarantees:

- One source version per state. A state's facilities all come from v3.0 or all from v2.0, never a mix.
- `facility_id` is unique. No facility is counted twice.

## `stg_version_lineage`

The cross-version facility lineage. Each v2.0 facility is reconstructed against v3.0 by a mutual nearest-neighbour match within 100 m on lat/lon, then classified. One row per facility, only for states v3.0 covers (a state still on v2.0 has no v3 to compare against, so it carries no lineage and is excluded).

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `facility_id` | varchar | no | unique; the `v3_id` for a carried-over or appeared record, the `v2_id` for a dropped one |
| `lineage_status` | varchar | no | one of `carried-over`, `dropped`, `appeared` |
| `state` | varchar | no | canonical state; always a v3.0-covered state |
| `lga` | varchar | yes | source LGA label (v3 side for carried-over/appeared, v2 side for dropped); frame-resolved in the mart, not here |
| `v2_id` | varchar | yes | `globalid` of the v2.0 facility; null for an appeared record |
| `v3_id` | varchar | yes | `unique_id` of the v3.0 facility; null for a dropped record |
| `v2_name` | varchar | yes | facility name on the v2.0 side |
| `v3_name` | varchar | yes | facility name on the v3.0 side |
| `match_distance_m` | double | yes | metres between the paired facilities; null unless carried-over |
| `name_similarity` | double | yes | Jaro-Winkler similarity of the two names; null unless carried-over |
| `name_match_band` | varchar | yes | banded `name_similarity`: `exact-or-near` (>=0.92), `close` (>=0.80), `divergent` (<0.80); null unless carried-over |

Guarantees:

- `facility_id` is unique. No facility appears in two lineage records.
- The match is a single mutual claim. Each v3 facility is paired with its one nearest v2 and vice versa, so no facility is claimed twice.
- A carried-over pair is always within 100 m. The radius ceiling holds in the built output, not just the model text.
- Field shape follows status. A carried-over record carries both ids plus distance and similarity; a dropped record carries the v2 id only; an appeared record carries the v3 id only. The match fields are null for anything that did not match.
- `dropped` and `appeared` are review candidates, not asserted facts. A drop may be a real closure or a sub-100 m move the match missed; an appearance may be genuinely new or a re-survey of the same site. Read them as the start of a reconciliation queue, never as a measured count of closures or openings.

## `stg_ntd_endemicity_lga`

ESPEN endemicity per LGA for five diseases (STH, schistosomiasis, lymphatic filariasis, onchocerciasis, Loa loa), each status ranked on one shared ordinal scale. Built from the derived per-LGA ESPEN table and the `ntd_endemicity_rank` seed. Derived from ESPEN (CC BY-NC-SA 3.0 IGO): non-commercial use only.

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `state` | varchar | no | the LGA's state or FCT |
| `canonical_lga` | varchar | no | canonical LGA name; the LGA key is `(state, canonical_lga)` |
| `state_key` | varchar | no | normalised state, the join key the marts use |
| `lga_key` | varchar | no | normalised LGA name, the join key the marts use |
| `espen_iu_id` | bigint | no | the ESPEN implementation unit the LGA maps to (exactly one per LGA) |
| `disease` | varchar | no | full disease name |
| `disease_code` | varchar | no | one of `STH`, `SCH`, `lf`, `Oncho`, `Loa` (ESPEN's codes as published) |
| `endemicity_latest_known` | varchar | yes | latest label that is not an "Unknown ..." status; null when the LGA was never reported with a known status |
| `year_latest_known` | integer | yes | year of that label; null exactly when the label is null |
| `endemicity_rank` | integer | yes | 0-3 (see the scale below); null exactly when `endemicity_latest_known` is null |
| `rank_basis` | varchar | no | `prevalence band` (STH, SCH, Loa) or `programme stage` (LF, Oncho) |
| `endemicity_latest` | varchar | no | the latest year's label as published, which may be an "Unknown ..." status |
| `year_latest` | integer | no | year of `endemicity_latest` |
| `years_reported` | integer | no | number of distinct years ESPEN reported this LGA and disease |

The rank scale. 0 is non-endemic and 3 the highest need. STH, schistosomiasis and Loa loa rank by prevalence band (low or hypo-endemic 1, moderate or meso-endemic 2, high or hyper-endemic 3). Lymphatic filariasis and onchocerciasis are published as programme stages, not prevalence, so they rank by stage: under post-intervention surveillance 1; under MDA, under targeted treatment, or pending impact assessment 2; endemic with MDA not delivered 3. Ranks order need within a disease; across the two bases they are comparable as an order of need, not as a prevalence figure.

Guarantees:

- Exactly one row per (LGA, disease): every one of the 774 frame LGAs appears for every disease.
- Every known label is ranked. A label ESPEN publishes that the seed does not rank fails the build, rather than silently becoming a null rank that reads as unknown.
- Unknown is not zero. An LGA whose only reports are "Unknown ..." statuses carries a null label, year and rank. A recent "Unknown" does not erase an earlier known status: the rank follows the latest known label, and the latest published label is kept alongside in `endemicity_latest`.

## `stg_access_population_lga`

Per-LGA population (WorldPop R2025A) and population-weighted travel time to healthcare (Malaria Atlas Project) for the one population year the register describes. The raw table carries one row per LGA for every pinned WorldPop year (2025-2030); this model selects one.

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `shapeID` | varchar | no | the frame LGA's geoBoundaries id; unique |
| `shapeName` | varchar | yes | the LGA name in the boundary set |
| `population_year` | bigint | no | the selected year, the same on every row |
| `population` | bigint | no | WorldPop population of the LGA for that year; always positive |
| `motorised_minutes`, `walking_minutes` | double | yes | population-weighted mean travel time to the nearest health facility; null where no population sits on valid travel-time cells |
| `walking_over_60min_share`, `access_population_share` | double | yes | shares, 0-1 |

Guarantees:

- Exactly one population year, covering all 774 frame LGAs.
- The year is chosen, not assumed: the `population_year` dbt var when set (a build of a year the table does not carry fails), otherwise the calendar year of the build, capped to the years available. A weekly refresh therefore moves to the new year on 1 January, and stays on the last available year once the table runs out rather than failing.
- Every year in the raw table covers the same 774 LGAs, once each, so whichever year is selected is complete.

## `lga_coverage_burden`

Per-LGA facility coverage crossed with NTD burden. One row per LGA, all 774 present.

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `canonical_lga` | varchar | no | canonical LGA name; not unique alone, several recur across states (see key rule below) |
| `state` | varchar | no | the LGA's state or FCT |
| `lga_label` | varchar | no | `'<canonical_lga>, <state>'`, a unique, readable key for selecting one LGA; also carried by `lga_coverage_choropleth` and `lga_coverage_outlook` |
| `espen_iu_id` | bigint | yes | the ESPEN IU id where the frame carries one |
| `facilities` | bigint | no | count in the authority layer; `0` means none matched, which may be a name-join miss, never asserted as a true absence |
| `facility_source_version` | varchar | yes | `v2.0` or `v3.0`; null only if the LGA matched no facility |
| `facility_status` | varchar | no | `current`, `stale-awaiting-v3`, or `unmatched` |
| `sth_high_share_pct_state` | double | yes | state-level ESPEN STH high-prevalence share, broadcast to each LGA in the state; null where the state was not surveyed |
| `sth_survey_years_state` | varchar | yes | the survey-year span behind that share |
| `sth_endemicity_rank_lga` | integer | yes | per-LGA ESPEN STH endemicity rank, 0-3; null when never known (see `stg_ntd_endemicity_lga`) |
| `sch_endemicity_rank_lga` | integer | yes | as above, schistosomiasis |
| `lf_endemicity_rank_lga` | integer | yes | as above, lymphatic filariasis (programme-stage basis) |
| `oncho_endemicity_rank_lga` | integer | yes | as above, onchocerciasis (programme-stage basis) |
| `loa_endemicity_rank_lga` | integer | yes | as above, Loa loa |
| `population` | bigint | no | WorldPop R2025A population of the LGA for `population_year`, summed at 100 m; always positive |
| `population_year` | bigint | no | the year `population` and the travel-time columns describe, selected by `stg_access_population_lga` |
| `births` | double | no | WorldPop annual births in the LGA for 2015 (UN-adjusted); always positive |
| `facilities_per_10k_population` | double | yes | `facilities` x 10,000 / `population`; null where `facility_status = 'unmatched'` |
| `facilities_per_10k_births` | double | yes | `facilities` x 10,000 / `births`; null where `facility_status = 'unmatched'` |
| `facilities_primary`, `facilities_secondary`, `facilities_tertiary` | bigint | no | facilities in the LGA by GRID3 level |
| `facilities_level_unknown` | bigint | no | facilities whose level is blank, `Unknown` or missing; never assigned a level |
| `facilities_functional` | bigint | no | facilities recorded as Functional or Partially Functional (GRID3 v3.0 only) |
| `facilities_not_functional` | bigint | no | facilities recorded as Not-Functional or Closed (GRID3 v3.0 only) |
| `facilities_status_unknown` | bigint | no | facilities with no recorded status, including every facility in a v2.0 state |
| `secondary_tertiary_per_100k_population` | double | yes | (`facilities_secondary` + `facilities_tertiary`) x 100,000 / `population`; null where `facility_status = 'unmatched'` |
| `functional_status_known_share` | double | yes | share of the LGA's facilities with a recorded status, 0-1; 0 throughout v2.0 states; null where unmatched |
| `not_functional_share` | double | yes | `facilities_not_functional` over facilities with a recorded status, 0-1; null where no status is recorded, never 0 |
| `motorised_minutes` | double | yes | population-weighted mean travel time to the nearest health facility by motorised transport, minutes (Malaria Atlas Project, 2020 release); null where no population sits on valid travel-time cells, never 0 |
| `walking_minutes` | double | yes | as above, walking only |
| `walking_over_60min_share` | double | yes | share of the LGA's population more than 60 minutes' walk from the nearest facility, 0-1 |
| `access_population_share` | double | yes | share of the LGA's population on valid travel-time cells, i.e. the share the travel figures describe, 0-1 |
| `flood_affected_2022_state` | boolean | no | the LGA's state was flood-affected in the 2022 event (NASA MODIS); a STATE flag repeated to each LGA, not an LGA measurement |
| `flood_affected_2024` | boolean | no | the LGA was flood-affected in the 2024 event (NASA MODIS, LGA-level); an exposure flag, not a severity |
| `malaria_year` | bigint | no | the year the malaria columns describe (2025 today) |
| `pf_parasite_rate` | double | yes | population-weighted P. falciparum parasite rate in children aged 2-10 (PfPR2-10, 0-1), Malaria Atlas Project 202608 release; a model estimate, null means not estimated, never 0 |
| `pf_incidence_per_1000` | double | yes | population-weighted P. falciparum clinical cases per 1,000 people a year, same release; a model estimate, not reported cases |
| `hard_to_reach` | boolean | yes | `walking_over_60min_share >= 0.25`; null where that share is null |
| `priority_high_burden_thin_coverage` | boolean | no | true where burden is high and coverage is thin; see rule below |
| `access_risk_score` | integer | yes | 0-2: one point for `hard_to_reach`, one for `flood_affected_2024`; null where `hard_to_reach` is null |
| `ntd_diseases_known` | integer | no | how many of the five diseases have a known ESPEN status in the LGA, 0-5 |
| `priority_burden_points` | integer | yes | 0-3, the highest known endemicity rank across the five diseases; null only when no status is known |
| `priority_coverage_points` | integer | yes | 0-3 by national quartile of `facilities_per_10k_population` over the matched LGAs: 3 in the lowest quartile, 0 in the highest; a value on a boundary takes the less urgent band; null where the rate is null |
| `priority_malaria_points` | integer | yes | 0-3 by national quartile of `pf_parasite_rate`: 3 in the highest quartile, 0 in the lowest; a value on a boundary takes the more urgent band; null where the rate is null |
| `priority_score` | integer | yes | 0-11: `priority_burden_points` + `priority_coverage_points` + `access_risk_score` + `priority_malaria_points`; null when any part is null; higher is more urgent |
| `priority_tier` | varchar | yes | `high` (score 8-11), `medium` (6-7) or `low` (0-5); null where `priority_score` is null. The method and its sensitivity to the weights are recorded in [ADR 0004](adr/0004-priority-score-method.md) |
| `priority_reasons` | varchar | yes | why the LGA scores what it does, one clause per part, e.g. `burden 3 (highest of 3 known disease ranks) · coverage 3 (0.6 facilities per 10,000 people) · access 1 (flooded in 2024)`; null exactly where `priority_score` is null |
| `lga_summary` | varchar | no | a one-line summary of the LGA (facilities and register version, per-capita rate, the five endemicity ranks, travel time, flood flag, access risk, malaria, facility mix, priority score and original flag), missing values written out (`n/a`, `unknown`); the dashboard's map hover |

Guarantees:

- Exactly 774 rows, one per LGA. No LGA missing, none duplicated.
- Per-capita coverage is traceable. `population` and `births` equal their source tables for every LGA, and each rate recomputes from `facilities` and its denominator. A rate is null exactly when no facility matched the LGA, because a zero count there may be a name-join miss; it is never shown as a rate of 0. `facilities_per_10k_population` is the primary per-capita measure; the births rate uses an older (2015) denominator and is indicative.
- Access and flood columns are traceable. The travel-time columns equal the per-LGA access table, each flood flag equals its event's source row, `hard_to_reach` follows its 0.25 threshold, and `access_risk_score` recomputes from its two points. The score counts only LGA-resolution signals: the 2022 flood flag is state-level and marks 727 of 774 LGAs, so it is carried but not scored. A null score means travel time was not measured, never low risk. The score does not feed the priority flag, which is unchanged.
- The key is `(state, canonical_lga)`, not `canonical_lga` alone. Six LGA names recur across states (Bassa, Ifelodun, Irepodun, Nasarawa, Obi, Surulere), so joining on the bare name would collide two distinct places. Always carry the state.
- `facilities` is never null; `0` is an explicit "none matched", read with `facility_status = 'unmatched'` to tell a join miss from a real low count.
- `sth_high_share_pct_state` is a STATE figure repeated across the state's LGAs, not an LGA measurement. Treat it as such.
- The `*_endemicity_rank_lga` columns are true per-LGA measurements, equal to the rank in `stg_ntd_endemicity_lga` for that LGA and disease. A null means the LGA's status was never known, not that the disease is absent; a non-endemic LGA is `0`. They are non-commercial (ESPEN CC BY-NC-SA 3.0 IGO). They do not feed the priority flag, which is unchanged.
- The priority score is derived and traceable: each part, the sum and the tier recompute from the ranks, the coverage-rate quartiles, the access score and the malaria-rate quartiles, and a dbt test fails the build otherwise. Burden takes the highest known rank, so one severe burden is not outranked by several mild ones; `ntd_diseases_known` says how many diseases that rank was drawn from. Coverage and malaria points are relative (national quartiles recomputed at each refresh), so an LGA's points can shift as other LGAs change; the tier cut-offs are fixed on the score. Malaria is scored relatively because 729 of 774 LGAs fall in the same absolute prevalence band (10-35%), where fixed bands would not separate LGAs. Any unmeasured part leaves the score and tier null, never low. The score sits beside the original flag, which is unchanged.
- The readable text agrees with the columns it describes. `priority_reasons` names each part's points and is null exactly when the score is; `lga_summary` opens with the LGA's label and states its score (or `n/a`). A dbt test checks both, and that the map hover on `lga_coverage_choropleth` is the same text.
- Malaria columns are traceable. They equal the per-LGA malaria table (`data/derived/malaria_lga.csv`) for every LGA, describe one year, and stay in range (rate 0-1, incidence never negative); a dbt test checks all three. Each figure is weighted by WorldPop 2025 on its 100 m grid, so LGAs smaller than one 5 km MAP cell still get their value. They are model estimates that rank where malaria is relatively heavier; the parasite rate feeds the priority score through `priority_malaria_points`.
- Facility mix adds up. The four level counts sum to `facilities`, and so do the three functional-status counts. A v2.0 LGA records no status (GRID3 v2.0 has none), and each mix rate recomputes from its counts. Because status is recorded for under a third of facilities, `not_functional_share` describes only the facilities whose status is known; read it with `functional_status_known_share`.
- The original priority flag is derived, not a source value: true only when `sth_high_share_pct_state >= 40` and `facilities > 0` and `facilities` is in the bottom tercile of non-zero counts. A zero-facility LGA is never flagged `true`, even with high burden, by design: `facilities = 0` means none matched (`facility_status = 'unmatched'`), which may be a name-join miss rather than a real absence, so flagging it would assert thin coverage the data has not established. Such LGAs are surfaced through `facility_status = 'unmatched'` for reconciliation, not through the priority flag.

## `lga_coverage_outlook`

Per-LGA coverage for every WorldPop population year the register carries, holding today's facility count fixed: how per-capita coverage would change as the population grows if no facility were added. It is a planning baseline, not a forecast of facilities.

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `state`, `canonical_lga`, `lga_label` | varchar | no | the LGA, as in `lga_coverage_burden` |
| `population_year` | bigint | no | the projection year |
| `is_register_year` | boolean | no | true on the rows for the population year the published register describes |
| `is_last_projection_year` | boolean | no | true on the rows for the last WorldPop year carried (2030 today) |
| `population` | bigint | no | WorldPop R2025A population of the LGA for that year |
| `facilities` | bigint | no | today's facility count, the same in every year |
| `facility_status` | varchar | no | as in `lga_coverage_burden` |
| `facilities_per_10k_population` | double | yes | `facilities` x 10,000 / `population`; null where `facility_status = 'unmatched'` |
| `population_change_from_register_year` | double | yes | population over the register year's, minus 1 |
| `facilities_per_10k_change_from_register_year` | double | yes | the rate minus the register year's rate; null where the rate is null |

Guarantees:

- Every population year covers the 774 frame LGAs exactly once.
- The register-year rows equal `lga_coverage_burden` (population, facilities and rate), so the outlook starts from the published register.
- Facilities are held fixed across years, and every rate and change recomputes, null exactly where no facility matched.

## `lga_profile_facts`

Everything the register holds for one LGA as readable text, in display order: the dashboard's LGA profile shows the chosen LGA's rows. Keeping the wording in the mart puts it under the contract and the tests, and lets the dashboard show it as a plain table rather than rendered HTML.

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `lga_label` | varchar | no | the LGA, as in `lga_coverage_burden` |
| `fact_order` | integer | no | display order within the LGA, 1-20 |
| `section` | varchar | no | `LGA`, `Priority`, `Facilities`, `People and access`, `NTD endemicity rank (0-3)` or `Malaria (MAP <year>)` |
| `fact` | varchar | no | what the value describes, e.g. `Per 10,000 people`, `Population (WorldPop 2026)` |
| `value` | varchar | no | the value as text; a missing input is written out (`n/a`, `unknown`, `not measured`, `not scored`, `status not recorded`), never `0` |

Guarantees:

- Every LGA in `lga_coverage_burden` has exactly facts 1-20, and no other LGA appears.
- The priority-score fact equals `priority_score` and `priority_tier` (or `not scored`).
- Values are text for reading, not for computing: aggregate from `lga_coverage_burden` instead.

## `lga_version_drift`

The `stg_version_lineage` records made queryable against the canonical 774-LGA frame: the same grain, one row per facility-lineage record, with the source LGA resolved through the alias crosswalk and joined to the frame so the lineage answers "what changed in this LGA between versions" without the caller re-doing the name reconciliation.

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `facility_id` | varchar | no | unique; the `v3_id` for a carried-over or appeared record, the `v2_id` for a dropped one |
| `lineage_status` | varchar | no | one of `carried-over`, `dropped`, `appeared` |
| `canonical_lga` | varchar | yes | canonical LGA name from the frame; null when the source LGA label does not resolve to a frame LGA |
| `source_lga` | varchar | yes | the LGA label as it came from the source, before frame resolution; kept so a null `canonical_lga` still shows what needs reconciliation |
| `state` | varchar | no | the frame's state name, spelled exactly as the frame spells it (for example `Federal Capital Territory`), on every row whether or not the LGA resolved |
| `espen_iu_id` | bigint | yes | ESPEN IU id from the frame where the LGA resolved and the frame carries one |
| `v2_id` | varchar | yes | `globalid` of the v2.0 facility; null for an appeared record |
| `v3_id` | varchar | yes | `unique_id` of the v3.0 facility; null for a dropped record |
| `v2_name` | varchar | yes | facility name on the v2.0 side |
| `v3_name` | varchar | yes | facility name on the v3.0 side |
| `match_distance_m` | double | yes | metres between the paired facilities; null unless carried-over |
| `name_similarity` | double | yes | Jaro-Winkler similarity of the two names; null unless carried-over |
| `name_match_band` | varchar | yes | banded name similarity; null unless carried-over (see `stg_version_lineage`) |

Guarantees:

- One row per `stg_version_lineage` record, same `facility_id` set. The frame join never drops a lineage record or duplicates one.
- `facility_id` is unique. No facility appears twice.
- `canonical_lga` null means unresolved, not absent. The source carried an LGA label the crosswalk does not map to a frame LGA; read it as a reconciliation item, never as "this facility has no LGA". The unresolved label is kept in `source_lga` so a null `canonical_lga` is still actionable.
- One label per state. `state` is resolved from the lineage state through the frame, not from the LGA join, so a record with an unresolved LGA carries the same frame spelling as its resolved neighbours and a filter or group-by on `state` sees each state as one value. A test fails the build if any `state` is not a frame state name.
- The key is the facility, not the LGA. `canonical_lga` is not unique on its own (an LGA holds many facilities, and LGA names recur across states), so group by `(state, canonical_lga)` when rolling up and never treat the LGA name alone as a key.
- The status semantics carry over from `stg_version_lineage`: `dropped` and `appeared` are review candidates, not measured closures or openings.

## `lga_coverage_choropleth`

`lga_coverage_burden` with each LGA's boundary polygon attached for mapping. The coverage, burden and priority columns are exactly as in the coverage mart and follow the same rules. The grain differs: the vendored boundary set is split into single Polygons (a deck.gl polygon layer rejects MultiPolygons), so an LGA whose boundary has more than one part (the coastal and riverine LGAs with islands) appears once per part, each row repeating the LGA's coverage values. On the current vendored set that is 800 rows covering the 774 LGAs.

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `geometry_geojson` | varchar | no | one polygon part of the LGA boundary as GeoJSON text, from the vendored simplified geoBoundaries set, joined through the frame's `shapeID`. Text, not `json`: a json-typed column reaches a map client as a parsed object, which a layer that parses the string itself cannot read |
| `geometry_feature` | varchar | no | the same polygon part as a complete GeoJSON Feature string (`type`, `geometry`, `properties`), for the deck.gl Geojson layer; `properties` carries `canonical_lga`, `state`, `facilities`, `facility_status`, `sth_high_share_pct_state`, `priority_high_burden_thin_coverage`, the five `*_endemicity_rank_lga` ranks, `population`, `facilities_per_10k_population`, the travel-time columns, `flood_affected_2024`, `hard_to_reach`, `access_risk_score`, `ntd_diseases_known`, the two priority point columns, `priority_score` and `priority_tier` |

The other columns (`canonical_lga`, `state`, `lga_label`, `lga_summary`, `espen_iu_id`, `facilities`, `facility_source_version`, `facility_status`, `sth_high_share_pct_state`, `sth_survey_years_state`, `priority_high_burden_thin_coverage`, the five `*_endemicity_rank_lga` ranks, `population`, `facilities_per_10k_population`, `motorised_minutes`, `walking_minutes`, `walking_over_60min_share`, `flood_affected_2024`, `hard_to_reach`, `access_risk_score`, `ntd_diseases_known`, `priority_burden_points`, `priority_coverage_points`, `priority_malaria_points`, `priority_score` and `priority_tier`) are exactly as in `lga_coverage_burden` above.

Guarantees:

- Exactly 774 distinct `(state, canonical_lga)` pairs, so every LGA is on the map.
- Geometry is never null: a missing polygon would be a hole in the map, so the build and a test fail rather than ship one.
- Rows are polygon parts, not LGAs. Do not sum or count coverage columns across rows: an LGA with three parts would be counted three times. Aggregate from `lga_coverage_burden`, or take distinct `(state, canonical_lga)` first.

## `register_summary`

One row describing the published register as a whole, rebuilt on every refresh so a dashboard can render it as a live summary instead of a hand-written report.

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `built_at` | timestamp | no | when this register was built, which is the refresh that published it (build host's clock) |
| `lgas` | bigint | no | LGAs in `lga_coverage_burden`; always 774 |
| `states` | bigint | no | distinct states and FCT in the frame |
| `states_on_v3` | bigint | no | states whose facilities come from GRID3 v3.0 |
| `states_on_v2` | bigint | no | states still on GRID3 v2.0 |
| `states_without_facilities` | bigint | no | states none of whose LGAs matched a facility, so no version applies |
| `v3_source_date` | date | yes | release date of the v3.0 register in use; null if no state uses it |
| `v2_source_date` | date | yes | release date of the v2.0 register in use; null if no state uses it |
| `facilities` | bigint | no | facilities in the authority layer |
| `facilities_in_lgas` | bigint | no | facilities whose LGA resolved to a frame LGA (the sum of `lga_coverage_burden.facilities`) |
| `facilities_v3` | bigint | no | authority-layer facilities from v3.0 |
| `facilities_v2` | bigint | no | authority-layer facilities from v2.0 |
| `lgas_unmatched` | bigint | no | LGAs with `facility_status = 'unmatched'` |
| `priority_lgas` | bigint | no | LGAs with the original priority flag set |
| `priority_high_tier_lgas` | bigint | no | LGAs with `priority_tier = 'high'` |
| `population` | bigint | no | total WorldPop population across the 774 LGAs |
| `population_year` | bigint | no | year of that population figure |
| `hard_to_reach_lgas` | bigint | no | LGAs with `hard_to_reach` true |
| `facilities_secondary_tertiary` | bigint | no | facilities in LGAs whose level is Secondary or Tertiary |
| `facilities_level_unknown` | bigint | no | facilities in LGAs with no recorded level |
| `facilities_functional_status_known` | bigint | no | facilities in LGAs with a recorded functional status |
| `facilities_not_functional` | bigint | no | facilities recorded as Not-Functional or Closed |
| `flood_affected_2024_lgas` | bigint | no | LGAs with `flood_affected_2024` true |
| `sth_known_lgas`, `sch_known_lgas`, `lf_known_lgas`, `oncho_known_lgas`, `loa_known_lgas` | bigint | no | LGAs with a known ESPEN status (a rank) for that disease; the rest are unknown, not zero |
| `espen_latest_year` | integer | yes | latest year any ESPEN status is reported for |
| `lineage_carried_over`, `lineage_dropped`, `lineage_appeared` | bigint | no | `lga_version_drift` records by status |

Guarantees:

- Exactly one row.
- It agrees with the tables it summarises: `states_on_v3 + states_on_v2 + states_without_facilities = states`, `facilities_v3 + facilities_v2 = facilities`, and `facilities_in_lgas` equals the coverage mart's facility total and never exceeds `facilities`. The gap between `facilities` and `facilities_in_lgas` is the facilities whose LGA label the crosswalk could not place, shown rather than hidden.
- Unknown is not zero here either: a known-status count is the LGAs with a rank, never the LGAs without a positive one.

## `register_changes`

What changed against the register this one replaced. It is not a dbt model: dbt builds each register from scratch with no access to the previous one, so the publish step writes this table into the new register just before it goes live, comparing it with the live register on `(state, canonical_lga)`. It is promoted with the register it describes, so a restored register keeps its own record.

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `change_order` | integer | no | display order: 0 for the summary, then 1, 2, … with no gaps |
| `change` | varchar | no | one of `summary`, `priority tier`, `facility match`, `facilities` |
| `lga_label` | varchar | yes | `'<canonical_lga>, <state>'`; null on the summary row |
| `state`, `canonical_lga` | varchar | yes | the LGA's key in `lga_coverage_burden`; null on the summary row |
| `previous` | varchar | yes | the value in the replaced register: its input fingerprint on the summary row (null on a first publish), the tier, `no facility` / `facilities matched`, or the facility count |
| `current` | varchar | no | the same value in this register |
| `facilities_change` | bigint | yes | this register's facility count minus the previous one, on `facility match` and `facilities` rows |
| `detail` | varchar | yes | on the summary row: how many LGAs changed tier, match status and facility count, or why nothing was compared; null elsewhere |

Guarantees:

- Exactly one summary row, with `change_order` 0.
- Every LGA whose `priority_tier` changed is listed (moves to `high` first), and every LGA whose match status changed (to or from `facility_status = 'unmatched'`).
- The 25 LGAs whose facility count changed most are listed, largest first; the summary row gives the full count.
- On a first publish, or when the replaced register lacks the compared columns, only the summary row is written and its `detail` says why.
- An LGA whose key is in only one of the two registers cannot be compared; the summary's `detail` counts them.
- A register restored from an archive made before this table existed is given a summary-only record whose `detail` says so, and `previous` is null.
- The live register is only read. If this table cannot be written, the publish fails and the live register stays as it was.

## The rule that spans every table

Missing is not zero. A stale state, an unsurveyed ESPEN IU, an LGA that matched no facility, and a dropped-from-v2 facility are each an absence. The schema records them as null, an explicit status, or a flagged review item, never as a measured zero.

## Freshness and versioning

- Freshness follows the source releases, not a clock. `source_date` on each facility says which GRID3 vintage it came from; a state marked `stale-awaiting-v3` is waiting for GRID3 to extend v3.0 coverage, not for a pipeline run.
- The population year is the one exception: it follows the build's calendar year within the WorldPop years the register carries (see `stg_access_population_lga`), and `register_summary.population_year` states which year a register uses.
- Newer upstream releases are watched, not ingested. A monthly Dagster job (`check_upstream_releases`) compares WorldPop, GRID3 on HDX, the Malaria Atlas Project (travel time, and the malaria parasite-rate and incidence releases) and ESPEN with what `data/sources.yml` pins and warns when something newer appears. A new release reaches the register only through a reviewed change to the pins and the committed tables. The per-LGA ESPEN table is rebuilt from ESPEN's public download route by `scripts/build_espen_lga.py`, which refuses to overwrite it unless the rebuild matches its pinned checksum or `--update` is given; the flood-exposure flags are a fixed snapshot pinned by checksum and not refreshed by the pipeline.
- The contract is versioned with the project (SemVer). Adding a column or relaxing nullability is a minor bump. Removing or renaming a column, tightening a domain, or changing a grain is a major bump and is announced in the CHANGELOG before release.
- dbt model contracts enforce the column names and types at build time. A model whose output drifts from the declared contract fails the build rather than shipping a silent schema change.
- A data-quality gate backs the guarantees a type cannot express. Blocking dbt tests fail the build when the raw inputs fall outside their row-count bounds, when the version-drift mart resolves an LGA absent from the frame, or when the NTD burden table or the alias crosswalk names a state the frame does not know; a blocking run check fails the pipeline when the carry-over rate collapses past a hard floor. These stop a bad build from publishing, as distinct from the advisory run metrics, which only warn. See docs/ARCHITECTURE.md.
