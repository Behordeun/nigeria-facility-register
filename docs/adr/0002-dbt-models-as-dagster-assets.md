# 0002. Model each dbt node as a Dagster asset, and publish in a separate terminal asset

- Status: accepted
- Date: 2026-11-09

## Context

The refresh had two Dagster assets. `raw_sources` loaded a run-scoped snapshot of the inputs into a side DuckDB file, and `dbt_models` ran `dbt build` against that file as one subprocess, then enforced the carry-over gate and promoted the side file onto the live register with one atomic rename. Dagster saw the whole dbt project as a single box: the asset graph did not show which mart depends on which staging model, a failed run said only that `dbt build` exited non-zero, and the dbt tests were not visible as checks on the models they test.

Points that shape the decision:

- The atomic publish is the property the refresh exists to keep. A reader of the live register must see the whole previous register or the whole new one, never a half-built, partly tested or gate-rejected one. Any split must still build into the side file and publish only after every model, every test and the gate pass.
- dagster-dbt maps a dbt project onto assets from its manifest: one asset per model and seed, one asset check per single-model test, with dependencies taken from `ref` and `source`. It runs `dbt build` as one invocation, so the build still behaves as one unit.
- dagster-dbt pins dagster exactly and caps dbt-core. 0.29.22, with dagster 1.13.22, is the first release whose range admits the dbt-core 1.12 security floor this project requires.
- The manifest has to exist when the Dagster code loads. It is a build artifact, not committed.
- The refresh job is imported by the sensor's unit tests, which run in CI before any dbt build has produced a manifest.

## Decision

Build the dbt project through dagster-dbt's `@dbt_assets`, and move the gate and publish into a new terminal asset, `published_register`:

1. `raw_sources` snapshots the inputs and loads them into the run's side database, as before.
2. `register_models` builds every dbt model and seed into that side database, one asset each, with each single-model dbt test as an asset check on its model. Every dbt source maps onto `raw_sources`, since the loader writes all the raw tables in one step. A failed model or test fails the step and discards the side database and the run's snapshot.
3. `published_register` enforces the carry-over gate on the side database, promotes it with the same archive-then-rename, clears the dashboard cache, and records the run's figures. The resolve-rate and carry-over asset checks attach to it, because it is the asset that reads the published file.

All of these sit in one asset group, `refresh`, and the refresh job selects that group by name, so the job module does not import the manifest. The dbt profile still reads the database path from `NFR_DUCKDB_PATH`; since dagster-dbt passes each invocation a copy of the process environment with no per-call override, the step sets the variable for the duration of the build and restores it. The deployment image runs `dbt parse` at build time, `dagster dev` parses on start, and CI loads the definitions after its sample dbt build.

## Consequences

- The asset graph shows the real model lineage, and a failed refresh names the model or test that failed and records how long each took.
- Materialising a dbt model asset means it was built into a run's side database, not that it is live. `published_register` is the asset whose materialisation means a register was published; freshness checks or consumers that care about what is live should read it, not the model assets.
- A singular dbt test that reads several models (most of the cross-mart consistency tests) still runs inside the build and still fails it, but dagster-dbt records it as an asset observation, not an asset check, since it has no single model to attach to. The asset checks therefore cover the single-model tests; the run log covers every test.
- A dbt model materialised on its own from the asset graph fails with a clear message, because no side database exists outside a full refresh run. Partial rebuilds are not supported; the register is always rebuilt whole, as before.
- dagster, dagster-webserver and dagster-dbt now move together, pinned exactly, and a dbt-core upgrade must wait for a dagster-dbt release that admits it.
- Setting the environment variable is safe only because each refresh runs in its own process and the concurrency key admits one refresh at a time. Running two refreshes in one process (an in-process executor with concurrency) would need a different way to pass the path, such as a per-run profiles directory.
