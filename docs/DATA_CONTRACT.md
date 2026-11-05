# Data contract

This is the promise the published tables make to anyone who reads them. It covers the two serving marts (`lga_coverage_burden` and `lga_version_drift`) and the staging layers they are built on. Column types are enforced by dbt model contracts (`contract: enforced` in the schema files); the guarantees below that a type cannot express are enforced by dbt tests. A change that breaks any guarantee here is a breaking change and follows the versioning rule at the end.

## Scope and grain

| Table | Layer | Grain | Rows |
| --- | --- | --- | --- |
| `stg_facilities_authority` | staging | one facility | one per facility in the authoritative set |
| `stg_version_lineage` | staging | one facility-lineage record | one per v2.0/v3.0 facility in a v3-covered state |
| `lga_coverage_burden` | mart | one LGA | exactly 774 |
| `lga_version_drift` | mart | one facility-lineage record | one per `stg_version_lineage` record |

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
| `priority_high_burden_thin_coverage` | boolean | no | true where burden is high and coverage is thin; see rule below |

Guarantees:

- Exactly 774 rows, one per LGA. No LGA missing, none duplicated.
- The key is `(state, canonical_lga)`, not `canonical_lga` alone. Six LGA names recur across states (Bassa, Ifelodun, Irepodun, Nasarawa, Obi, Surulere), so joining on the bare name would collide two distinct places. Always carry the state.
- `facilities` is never null; `0` is an explicit "none matched", read with `facility_status = 'unmatched'` to tell a join miss from a real low count.
- `sth_high_share_pct_state` is a STATE figure repeated across the state's LGAs, not an LGA measurement. Treat it as such.
- The priority flag is derived, not a source value: true only when `sth_high_share_pct_state >= 40` and `facilities > 0` and `facilities` is in the bottom tercile of non-zero counts. A zero-facility LGA is never flagged `true`, even with high burden, by design: `facilities = 0` means none matched (`facility_status = 'unmatched'`), which may be a name-join miss rather than a real absence, so flagging it would assert thin coverage the data has not established. Such LGAs are surfaced through `facility_status = 'unmatched'` for reconciliation, not through the priority flag.

## `lga_version_drift`

The `stg_version_lineage` records made queryable against the canonical 774-LGA frame: the same grain, one row per facility-lineage record, with the source LGA resolved through the alias crosswalk and joined to the frame so the lineage answers "what changed in this LGA between versions" without the caller re-doing the name reconciliation.

| Column | Type | Nullable | Domain / rule |
| --- | --- | --- | --- |
| `facility_id` | varchar | no | unique; the `v3_id` for a carried-over or appeared record, the `v2_id` for a dropped one |
| `lineage_status` | varchar | no | one of `carried-over`, `dropped`, `appeared` |
| `canonical_lga` | varchar | yes | canonical LGA name from the frame; null when the source LGA label does not resolve to a frame LGA |
| `source_lga` | varchar | yes | the LGA label as it came from the source, before frame resolution; kept so a null `canonical_lga` still shows what needs reconciliation |
| `state` | varchar | no | the frame's state where the LGA resolved, else the lineage state |
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
- The key is the facility, not the LGA. `canonical_lga` is not unique on its own (an LGA holds many facilities, and LGA names recur across states), so group by `(state, canonical_lga)` when rolling up and never treat the LGA name alone as a key.
- The status semantics carry over from `stg_version_lineage`: `dropped` and `appeared` are review candidates, not measured closures or openings.

## The rule that spans every table

Missing is not zero. A stale state, an unsurveyed ESPEN IU, an LGA that matched no facility, and a dropped-from-v2 facility are each an absence. The schema records them as null, an explicit status, or a flagged review item, never as a measured zero.

## Freshness and versioning

- Freshness follows the source releases, not a clock. `source_date` on each facility says which GRID3 vintage it came from; a state marked `stale-awaiting-v3` is waiting for GRID3 to extend v3.0 coverage, not for a pipeline run.
- The contract is versioned with the project (SemVer). Adding a column or relaxing nullability is a minor bump. Removing or renaming a column, tightening a domain, or changing a grain is a major bump and is announced in the CHANGELOG before release.
- dbt model contracts enforce the column names and types at build time. A model whose output drifts from the declared contract fails the build rather than shipping a silent schema change.
