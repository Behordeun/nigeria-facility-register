# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims for [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Version-aware authoritative facility layer: v3.0 where it covers a state, v2.0 elsewhere, each row carrying its source version and current/stale status.
- Cross-version facility lineage as modelled, tested tables: a staging model (stg_version_lineage) that reconstructs each v2.0 facility against v3.0 by a mutual nearest-neighbour match within 100 m on lat/lon and classifies it carried-over, dropped or appeared with a match distance and a Jaro-Winkler name-similarity band, and a serving mart (lga_version_drift) that resolves the lineage against the canonical 774-LGA frame so it is queryable by LGA. Both carry enforced column contracts and tests: unique facility key, accepted status and name-band domains, a per-status field-shape check, a 100 m radius check on the built output, a grain-preservation check between staging and mart, and a fixture check that all three statuses are exercised.
- Per-LGA mart joining facility coverage to ESPEN soil-transmitted-helminths burden on the 774-LGA frame.
- DuckDB source loader and a two-stage dbt transform (staging, marts) with schema tests for the data contract.
- Unit tests pinning the LGA and state name-normalisation keys.
- CI running lint, unit tests and a dbt parse.
- Data contract for the published marts: DATA_CONTRACT.md documents the grain, keys, column types, nullability, value domains, the "missing is not zero" rule, and freshness/versioning. Enforced by dbt model contracts plus tests, including a 774-LGA row-count check and a composite (state, canonical_lga) uniqueness check on the coverage mart.
- Committed sample fixture (two states) so CI loads it and runs the full dbt build, proving the contract on every PR rather than only parsing. Toolchain pinned to Python 3.12 with a uv lockfile and dbt-duckdb 1.9.6.
- Dagster refresh pipeline: assets for the source load and the dbt build, a weekly schedule, and a sensor that rebuilds only when a new GRID3 vintage lands in data/raw/. Dagster is an optional install (the orchestration extra).
- Licensing split: AGPL-3.0-or-later code, CC BY derived artifacts, non-commercial terms carried for the derived ESPEN summary. Raw inputs fetched, not committed.
- LGA alias crosswalk mapping facility-source LGA spellings and former names to the canonical frame name, loaded and applied in the mart so a known variant joins instead of reporting zero.
- Coverage reporting to SonarCloud: pytest-cov emits coverage.xml, a sonar-project.properties uploads it, and CI runs the scan pinned to a commit SHA. Coverage reflects the unit-tested library code; the I/O loader and removed prototype scripts are not counted.

### Fixed

- The authority switch no longer drops every v2 fallback facility when the v3 source contains a null state, and canonicalises v2/v3 state-label aliases so a covered state is not duplicated or left with zero-count LGAs.
- Added tests for exact frame-key coverage, facility_status/source_version consistency, and both authority versions being present, so a dropped LGA, a contradictory coverage state, or a fixture that loses a path is caught.
- Source dates are the data vintage end dates, not publication dates, so freshness is never reported as a future date. The CSV loader parses deterministically, validates source paths resolve within the project, and loads all tables in one transaction.
- The lineage anti-joins are null-safe: dropped and appeared are computed with NOT EXISTS rather than NOT IN, and source ids are filtered non-null at the v2/v3 CTEs, so a null identifier can never empty the unmatched sets. Name similarity no longer coerces a null facility name to an empty string, so an absent name yields a null score and band rather than a spurious divergent classification. The version-drift mart keeps the unresolved source LGA label (source_lga) alongside canonical_lga and reduces the frame to one row per (state, LGA) key before joining, so an unresolved label stays visible and a duplicate frame row cannot fan out the lineage grain. The grain-preservation test compares the staging and mart facility-id sets in both directions instead of only row counts, and the all-statuses fixture check runs only on the sample build (var fixture_build) so a real refresh with fewer statuses is not rejected.

### Removed

- Three superseded prototype scripts (build_authority_layer, diff_versions, match_v2_v3_spatial) that nothing imported; the dbt models replace them.

### Notes

- The project is pre-release. The public interface and the mart schema may change before the first tagged version.
