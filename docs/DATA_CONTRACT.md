# Data contract

This is the promise the published tables make to anyone who reads them. It covers the four serving marts (`lga_coverage_burden`, `lga_version_drift`, `lga_coverage_choropleth` and `register_summary`) and the staging layers they are built on. Column types are enforced by dbt model contracts (`contract: enforced` in the schema files); the guarantees below that a type cannot express are enforced by dbt tests. A change that breaks any guarantee here is a breaking change and follows the versioning rule at the end.

## Scope and grain

| Table | Layer | Grain | Rows |
| --- | --- | --- | --- |
| `stg_facilities_authority` | staging | one facility | one per facility in the authoritative set |
| `stg_version_lineage` | staging | one facility-lineage record | one per v2.0/v3.0 facility in a v3-covered state |
| `stg_ntd_endemicity_lga` | staging | one (LGA, disease) | exactly 774 x 5 = 3,870 |
| `lga_coverage_burden` | mart | one LGA | exactly 774 |
| `lga_version_drift` | mart | one facility-lineage record | one per `stg_version_lineage` record |
| `register_summary` | mart | the published register | exactly 1 |
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

## `lga_coverage_burden`

Per-LGA facility coverage crossed with NTD burden. One row per LGA, all 774 present.

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `canonical_lga` | varchar | no | canonical LGA name; not unique alone, several recur across states (see key rule below) |
| `state` | varchar | no | the LGA's state or FCT |
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
| `priority_high_burden_thin_coverage` | boolean | no | true where burden is high and coverage is thin; see rule below |

Guarantees:

- Exactly 774 rows, one per LGA. No LGA missing, none duplicated.
- The key is `(state, canonical_lga)`, not `canonical_lga` alone. Six LGA names recur across states (Bassa, Ifelodun, Irepodun, Nasarawa, Obi, Surulere), so joining on the bare name would collide two distinct places. Always carry the state.
- `facilities` is never null; `0` is an explicit "none matched", read with `facility_status = 'unmatched'` to tell a join miss from a real low count.
- `sth_high_share_pct_state` is a STATE figure repeated across the state's LGAs, not an LGA measurement. Treat it as such.
- The `*_endemicity_rank_lga` columns are true per-LGA measurements, equal to the rank in `stg_ntd_endemicity_lga` for that LGA and disease. A null means the LGA's status was never known, not that the disease is absent; a non-endemic LGA is `0`. They are non-commercial (ESPEN CC BY-NC-SA 3.0 IGO). They do not feed the priority flag, which is unchanged.
- The priority flag is derived, not a source value: true only when `sth_high_share_pct_state >= 40` and `facilities > 0` and `facilities` is in the bottom tercile of non-zero counts. A zero-facility LGA is never flagged `true`, even with high burden, by design: `facilities = 0` means none matched (`facility_status = 'unmatched'`), which may be a name-join miss rather than a real absence, so flagging it would assert thin coverage the data has not established. Such LGAs are surfaced through `facility_status = 'unmatched'` for reconciliation, not through the priority flag.

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
| `geometry_feature` | varchar | no | the same polygon part as a complete GeoJSON Feature string (`type`, `geometry`, `properties`), for the deck.gl Geojson layer; `properties` carries `canonical_lga`, `state`, `facilities`, `facility_status`, `sth_high_share_pct_state`, `priority_high_burden_thin_coverage` and the five `*_endemicity_rank_lga` ranks |

The other columns (`canonical_lga`, `state`, `espen_iu_id`, `facilities`, `facility_source_version`, `facility_status`, `sth_high_share_pct_state`, `sth_survey_years_state`, `priority_high_burden_thin_coverage`, and the five `*_endemicity_rank_lga` ranks) are exactly as in `lga_coverage_burden` above.

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
| `priority_lgas` | bigint | no | LGAs with the priority flag set |
| `sth_known_lgas`, `sch_known_lgas`, `lf_known_lgas`, `oncho_known_lgas`, `loa_known_lgas` | bigint | no | LGAs with a known ESPEN status (a rank) for that disease; the rest are unknown, not zero |
| `espen_latest_year` | integer | yes | latest year any ESPEN status is reported for |
| `lineage_carried_over`, `lineage_dropped`, `lineage_appeared` | bigint | no | `lga_version_drift` records by status |

Guarantees:

- Exactly one row.
- It agrees with the tables it summarises: `states_on_v3 + states_on_v2 + states_without_facilities = states`, `facilities_v3 + facilities_v2 = facilities`, and `facilities_in_lgas` equals the coverage mart's facility total and never exceeds `facilities`. The gap between `facilities` and `facilities_in_lgas` is the facilities whose LGA label the crosswalk could not place, shown rather than hidden.
- Unknown is not zero here either: a known-status count is the LGAs with a rank, never the LGAs without a positive one.

## The rule that spans every table

Missing is not zero. A stale state, an unsurveyed ESPEN IU, an LGA that matched no facility, and a dropped-from-v2 facility are each an absence. The schema records them as null, an explicit status, or a flagged review item, never as a measured zero.

## Freshness and versioning

- Freshness follows the source releases, not a clock. `source_date` on each facility says which GRID3 vintage it came from; a state marked `stale-awaiting-v3` is waiting for GRID3 to extend v3.0 coverage, not for a pipeline run.
- The contract is versioned with the project (SemVer). Adding a column or relaxing nullability is a minor bump. Removing or renaming a column, tightening a domain, or changing a grain is a major bump and is announced in the CHANGELOG before release.
- dbt model contracts enforce the column names and types at build time. A model whose output drifts from the declared contract fails the build rather than shipping a silent schema change.
- A data-quality gate backs the guarantees a type cannot express. Blocking dbt tests fail the build when the raw inputs fall outside their row-count bounds, when the version-drift mart resolves an LGA absent from the frame, or when the NTD burden table or the alias crosswalk names a state the frame does not know; a blocking run check fails the pipeline when the carry-over rate collapses past a hard floor. These stop a bad build from publishing, as distinct from the advisory run metrics, which only warn. See docs/ARCHITECTURE.md.
