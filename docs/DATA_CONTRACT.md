# Data contract

This is the promise the published tables make to anyone who reads them. It covers the one implemented serving mart (`lga_coverage_burden`) and the staging authority layer it is built on. The `lga_version_drift` mart is planned, not yet built, and its contract is marked as such below. Column types are enforced by dbt model contracts (`contract: enforced` in the schema files); the guarantees below that a type cannot express are enforced by dbt tests. A change that breaks any guarantee here is a breaking change and follows the versioning rule at the end.

## Scope and grain

| Table | Layer | Grain | Rows |
| --- | --- | --- | --- |
| `stg_facilities_authority` | staging | one facility | one per facility in the authoritative set |
| `lga_coverage_burden` | mart | one LGA | exactly 774 |
| `lga_version_drift` | mart (planned) | one facility-lineage record | one per matched or unmatched facility |

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

## `lga_version_drift` (planned)

The v2-to-v3 facility lineage promoted from a CSV to a modelled, queryable table. Grain is one lineage record per facility, classified carried-over / dropped / appeared, with the match distance and name-similarity band. Full column spec lands with the lineage-mart branch.

## The rule that spans every table

Missing is not zero. A stale state, an unsurveyed ESPEN IU, an LGA that matched no facility, and a dropped-from-v2 facility are each an absence. The schema records them as null, an explicit status, or a flagged review item, never as a measured zero.

## Freshness and versioning

- Freshness follows the source releases, not a clock. `source_date` on each facility says which GRID3 vintage it came from; a state marked `stale-awaiting-v3` is waiting for GRID3 to extend v3.0 coverage, not for a pipeline run.
- The contract is versioned with the project (SemVer). Adding a column or relaxing nullability is a minor bump. Removing or renaming a column, tightening a domain, or changing a grain is a major bump and is announced in the CHANGELOG before release.
- dbt model contracts enforce the column names and types at build time. A model whose output drifts from the declared contract fails the build rather than shipping a silent schema change.
