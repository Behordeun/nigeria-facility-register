# Nigeria Facility Register

A version-aware health-facility register for Nigeria, built from GRID3's published releases and composed with neglected-tropical-disease burden at LGA resolution.

GRID3 publishes its health-facility data in versions that cover different states and share no common identifier. v2.0 is national (51,022 facilities, to October 2024). v3.0 covers 24 states (41,778 facilities, to December 2025) and supersedes v2.0 in those states. This project keeps each state on its most current version, reconstructs how facilities change between versions, and joins the result to disease burden.

## What it produces

- A single authoritative facility layer that switches source per state: v3.0 where it exists, v2.0 elsewhere. Every row records its source version and whether that state's data is current or stale.
- A v2-to-v3 lineage table. The two versions share no carry-over id (v2 keys on `globalid`, v3 on `unique_id`), so lineage is rebuilt by spatial and name matching. 89.8% of v2.0 facilities match a v3.0 point within 100 m, with 89.9% name agreement.
- A per-LGA table joining facility coverage to ESPEN soil-transmitted-helminths burden, flagging LGAs where burden is high and coverage is thin.

## How it is built

A loader reads each source file into DuckDB as a `raw` table. Staging builds the version-aware authority layer and the v2-to-v3 match. Marts join coverage to burden on the 774-LGA frame. dbt tests enforce the contract: one source version per state, unique facility ids, no silent zeros. The whole thing runs locally on DuckDB and dbt.

## Layout

```text
src/nfr/          Python: name keys, source loader, Dagster orchestration
dbt/              DuckDB models: staging (authority layer, version lineage), marts (coverage x burden, version drift)
data/frame/       774-LGA index + centroids (CC BY)
data/derived/     derived ESPEN NTD summary (CC BY-NC-SA, non-commercial)
data/raw/         fetched geopackages (gitignored, see data/sources.yml)
scripts/          fetch_sources.py
tests/            unit tests for the join keys
```

## Run it

```bash
make setup                     # venv + deps
# download the two GRID3 geopackages in a browser (links in data/sources.yml), then:
python scripts/fetch_sources.py --place ~/Downloads/nga_health_facilities_v2_0.gpkg
python scripts/fetch_sources.py --place ~/Downloads/grid3_nga_health_facilities_v3_0.gpkg
make fetch                     # verify they are present and valid
make build                     # load sources + dbt staging + marts
make test                      # unit + dbt tests
```

The raw geopackages are fetched, not committed. HDX serves them behind a challenge that blocks scripted download, so download them in a browser using the links in `data/sources.yml`. Place each download with `scripts/fetch_sources.py --place <file>`: it checks the file is a complete, readable geopackage and moves it into `data/raw/` with one atomic rename, so the loader and the refresh sensor never see a half-written file at the final path.

## Data honesty

- Missing is not zero: a stale state, an unsurveyed ESPEN IU, and an LGA whose name did not join are each reported, never counted as zero. About 97% of facilities join to an LGA; the rest are flagged for name reconciliation.
- Dropped and appeared facilities between versions are review queues, not verdicts.
- NTD burden is state-level and broadcast to each LGA in the state, labelled as such.

## Data contract

The published marts carry a written, tested contract: column types are enforced by dbt model contracts, and the guarantees a type cannot express (grain, keys, "missing is not zero", value domains, freshness) are enforced by tests. See `docs/DATA_CONTRACT.md`.

## Licensing

Code is AGPL-3.0-or-later (`LICENSE`): you may use, modify and self-host it, and a modified version offered as a network service must publish its source under the same license. Derived data artifacts from CC BY sources are CC BY 4.0. The ESPEN NTD summary carries the upstream CC BY-NC-SA 3.0 IGO non-commercial terms. Raw inputs are not redistributed. See `LICENSE-DATA` and `data/ATTRIBUTION.md`.
