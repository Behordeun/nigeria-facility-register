# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims for [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Version-aware authoritative facility layer: v3.0 where it covers a state, v2.0 elsewhere, each row carrying its source version and current/stale status.
- Spatial v2.0-to-v3.0 facility match, since the two GRID3 versions share no carry-over id. Produces carry-over, dropped and appeared sets as review queues.
- Per-LGA mart joining facility coverage to ESPEN soil-transmitted-helminths burden on the 774-LGA frame.
- DuckDB source loader and a two-stage dbt transform (staging, marts) with schema tests for the data contract.
- Unit tests pinning the LGA and state name-normalisation keys.
- CI running lint, unit tests and a dbt parse.
- Data contract for the published marts: DATA_CONTRACT.md documents the grain, keys, column types, nullability, value domains, the "missing is not zero" rule, and freshness/versioning. Enforced by dbt model contracts plus tests, including a 774-LGA row-count check and a composite (state, canonical_lga) uniqueness check on the coverage mart.
- Committed sample fixture (two states) so CI loads it and runs the full dbt build, proving the contract on every PR rather than only parsing. Toolchain pinned to Python 3.12 with a uv lockfile and dbt-duckdb 1.9.6.
- Licensing split: AGPL-3.0-or-later code, CC BY derived artifacts, non-commercial terms carried for the derived ESPEN summary. Raw inputs fetched, not committed.

### Notes

- The project is pre-release. The public interface and the mart schema may change before the first tagged version.
