# Architecture

The pipeline turns Nigeria's versioned, conflicting health-facility registers into one point-in-time authoritative layer, tracks how facilities change between register versions, and joins coverage to disease burden at LGA resolution. It runs locally on DuckDB and dbt, orchestrated by Dagster.

## Data flow

```mermaid
flowchart TD
    subgraph sources["External sources (fetched, not committed)"]
        G2["GRID3 v2.0 geopackage<br/>national, CC BY"]
        G3["GRID3 v3.0 geopackage<br/>24 states, CC BY"]
        ES["ESPEN STH endemicity<br/>CC BY-NC-SA"]
    end

    subgraph vendored["Vendored in repo (small, derived)"]
        FR["774-LGA master index<br/>+ centroids, CC BY"]
        XW["LGA alias crosswalk"]
        NB["ESPEN per-state summary<br/>derived, non-commercial"]
    end

    subgraph raw["raw (DuckDB, immutable)"]
        R2["grid3_v2"]
        R3["grid3_v3"]
        RF["frame_master_index"]
        RX["lga_alias_crosswalk"]
        RN["ntd_sth_state"]
    end

    subgraph staging["staging (dbt)"]
        AUTH["stg_facilities_authority<br/>per-state v3/v2 switch<br/>contract + tests"]
        LIN["stg_version_lineage<br/>v2 to v3 spatial match<br/>carry-over / dropped / appeared"]
    end

    subgraph marts["marts (dbt, the product)"]
        COV["lga_coverage_burden<br/>coverage x NTD burden<br/>priority flag"]
        LINM["lga_version_drift<br/>queryable lineage"]
    end

    G2 --> R2
    G3 --> R3
    FR --> RF
    XW --> RX
    ES --> NB --> RN

    R2 --> AUTH
    R3 --> AUTH
    R2 --> LIN
    R3 --> LIN
    RX --> AUTH

    AUTH --> COV
    RF --> COV
    RN --> COV
    LIN --> LINM
    RF --> LINM

    COV --> OUT["Published marts<br/>DATA_CONTRACT.md"]
    LINM --> OUT
```

## Spatial matching (why lineage needs it)

v2.0 keys on `globalid`, v3.0 on `unique_id`. The two versions share no carry-over id and the NHFR bridge code is too sparse in v3.0, so a facility's lineage across versions is reconstructed by position and name.

```mermaid
flowchart LR
    V2["v2.0 facility<br/>globalid, lat/lon, name"]
    V3["v3.0 facility<br/>unique_id, lat/lon, name"]
    V2 --> NN{"nearest neighbour<br/>within 100 m,<br/>single claim"}
    V3 --> NN
    NN -->|matched| CARRY["carried over<br/>+ name-similarity band"]
    NN -->|v2 unmatched| DROP["candidate dropped<br/>review queue"]
    NN -->|v3 unmatched| APP["candidate appeared<br/>review queue"]
```

## Orchestration and run lifecycle

```mermaid
flowchart TD
    SENSOR["new_vintage_sensor<br/>fires on a new file fingerprint"]
    SCHED["weekly_refresh<br/>Monday 06:00"]
    SENSOR --> JOB
    SCHED --> JOB
    JOB["refresh_register job"] --> A1["asset: raw_sources<br/>load into DuckDB"]
    A1 --> A2["asset: dbt_models<br/>dbt build + test"]
    A2 --> CHECKS["asset checks + run metadata<br/>load counts, LGA resolve rate,<br/>carry-over rate (planned)"]
```

## Layer contracts

| Layer | Holds | Guarantee |
| --- | --- | --- |
| raw | source tables loaded as-is | immutable, reload is idempotent, provenance + hash pinned |
| staging | authority layer, version lineage | one source version per state, unique facility id, crosswalk-resolved LGA |
| marts | coverage-burden, version drift | one row per LGA, missing is not zero, enforced column contract (DATA_CONTRACT.md) |

## Boundaries that hold

- Raw third-party data is fetched, never committed. Derived outputs are Produced Works.
- ESPEN content stays non-commercial; only the derived per-state summary ships.
- Missing is not zero at every layer: a stale state, an unsurveyed IU, an unmatched LGA is an absence, never a measured zero.
- Every published mart has a written, tested contract before it is called done.
