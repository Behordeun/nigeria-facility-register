# Deployment

The dev and CI loops do not need a container: `uv` plus the committed lockfile
reproduce the environment, and `make sample` / `make build` run the transform
directly, into `build/scratch.duckdb`. Local builds never write
`build/nfr.duckdb`, the published register that only the Dagster refresh
promotes onto and that Superset reads. This document is for an **always-on deployment**, where the Dagster
schedule and sensor must keep running so the register refreshes on its own.

## What runs

Six services (`docker-compose.yml`). The first two share the project image (`Dockerfile`); the two Superset services share `nfr-superset:local`, built from `superset/Dockerfile`:

- **webserver** — the Dagster UI, on `127.0.0.1:3000`.
- **daemon** — runs the weekly schedule and the new-vintage sensor, and launches
  the `refresh_register` job.
- **superset** — Apache Superset, on `127.0.0.1:8088`, reading the register read-only for dashboards. See the Superset section below.
- **superset-worker** — a Celery worker that renders dashboard downloads (PDF and image) in headless Chromium.
- **redis** — the worker's queue, the cache for rendered downloads and the cache for chart results; not published on any host port.
- **superset-db** — Postgres holding Superset's own records (users, roles, charts, dashboards); not the register, and not published on any host port.

The Dagster webserver and daemon share a `DAGSTER_HOME` volume (one instance, one run store, one set of
schedule/sensor state) and the `build/` volume (the live DuckDB database, the
run-scoped staging databases, and the per-run snapshots). The refresh stays
serialised by the `QueuedRunCoordinator` in `dagster.yaml`, so the schedule, the
sensor, and a manual run never overlap on the shared database.

## State and inputs

| Path | Kind | Holds |
| --- | --- | --- |
| `dagster_home` volume | named volume | instance config, run storage, schedule/sensor state |
| `build` volume | named volume | `nfr.duckdb` (live), `build/staging/` (run-scoped), `build/snapshots/` |
| `./data/raw` | bind mount (read-only) | the GRID3 geopackages the operator places |

The raw geopackages are license-fetched and never baked into the image. Place
them on the host with `scripts/fetch_sources.py --place` (see the README), then
they are visible read-only inside the containers at `/app/data/raw`.

## Run it

```bash
# build the images and start every service
docker compose up -d --build

# the UI (localhost only)
open http://127.0.0.1:3000

# follow the daemon (schedule + sensor)
docker compose logs -f daemon

# stop
docker compose down            # keep volumes (data persists)
docker compose down -v         # also drop volumes (wipes the built register)
```

The first refresh runs when the weekly schedule fires or when the sensor sees a
new vintage in `data/raw/`; trigger one immediately from the UI (Assets ->
materialize, or Jobs -> `refresh_register` -> launch) once the geopackages are
placed.

The daemon also runs `monthly_upstream_check` (07:00 on the 1st), which launches the `check_upstream_releases` job. It reads public catalogue pages only, so it needs outbound HTTPS to data.worldpop.org, data.humdata.org, data.malariaatlas.org and espen.afro.who.int; without it the check reports those publishers as unreachable and the refresh is unaffected. A warning on its `no_unreviewed_upstream_release` check means a newer upstream release is waiting for review.

The population year needs no action: each refresh uses the current calendar year from the committed WorldPop years (2025-2030). To build a specific year, pass it to dbt, for example `dbt build --vars 'population_year: 2026'`.

The orchestrated job always builds from the real inputs in `data/raw/`. It has
no fixture mode, so `raw_sources` fails fast with a "missing raw inputs" message
when `data/raw/` is empty rather than building an empty register. The committed
two-state sample under `data/sample/` is for the local data build only: run it
with `make sample`, which loads the sample into DuckDB and runs dbt with
`fixture_build: true` in one step, never touching Dagster. The fixture vars
lower the row-count floors to the sample's size; the Dagster job does not set
them, so placing the sample in `data/raw/` and launching the job fails
`assert_raw_row_counts_sane` on the production minimums. So a green fixture build
does not make the Dagster job runnable, and the sample is not a shortcut for it:
the job needs the real HDX geopackages in `data/raw/`. Because `data/raw/` is a
read-only mount inside the containers, place those on the host with
`scripts/fetch_sources.py --place`, where the daemon and webserver then see them
through the mount with no restart.

## Security notes

- The webserver is **unauthenticated**. Inside the container the process binds
  `0.0.0.0` because Docker's published-port forwarding reaches a container over
  its bridge interface, not loopback, so a container-loopback bind would be
  unreachable even through the host mapping. The UI is kept off other hosts by
  publishing the port only on the host's `127.0.0.1` and putting both services
  on a private bridge network. **Do not drop the `127.0.0.1:` prefix on the
  webserver `ports:` entry** without putting an authenticating reverse proxy in
  front; that prefix is the only thing keeping the UI off every host interface.
- The base image is pinned to a SHA-256 digest, not just the floating tag, so a
  rebuild is reproducible; refresh the digest deliberately (see the Dockerfile
  comment) rather than letting the tag move.
- The `uv sync` steps use a BuildKit cache mount, so packages downloaded or built once (including the dbt parser wheel fetched from GitHub releases) are reused by later builds. `docker compose build` already rebuilds the dependency layer when `uv.lock` changes, so `--no-cache` is rarely needed; it starts from an empty cache mount, and the dependency step then retries up to three times if an upstream download fails. `docker builder prune` clears the cache.
- `data/raw` is mounted read-only, so a container cannot alter the operator's
  placed inputs.

## Superset (dashboards)

The compose stack includes an Apache Superset service. It reads the live register **read-only** (the `build` volume mounted at `/app/register`), so a dashboard query can never alter the published data.

The dashboard ships with the repo as a Superset import bundle in `superset/dashboards/nfr_coverage_burden/`: the read-only DuckDB connection, eight datasets (`lga_coverage_burden`, `lga_coverage_choropleth`, `lga_coverage_outlook`, `lga_profile_facts`, `lga_version_drift`, `stg_ntd_endemicity_lga`, `stg_facilities_authority`, `register_summary`), 33 charts and the layout, all keyed by stable UUIDs. "Nigeria facility coverage and NTD burden" has eight tabs, in the order a planner works: where to act, one LGA, the score's parts, then the context behind them and the data's provenance:

- **Coverage**: facilities in the register, LGAs matched and unmatched, high-priority LGAs; a ranked table of the high-priority LGAs, highest score and thinnest coverage first, with its malaria parasite rate and a plain-language reason for each part of the score, where selecting an LGA opens it on the LGA profile tab; maps of facilities per 10,000 people (LGAs with no matched facility left blank) and the 0-11 priority score (coloured in bands that follow the tiers); a note explaining the score; and a by-state table with population, the state's facilities per 10,000 people and its high-priority LGAs.
- **LGA profile**: everything the register holds for one LGA, chosen in the LGA filter (required; it opens on the highest-priority LGA): a table of its 20 facts from `lga_profile_facts`, covering facilities by level, per-capita rates, functional status, population, travel time, flood exposure, disease ranks and the priority score with its four parts, beside a line of the LGA's facilities per 10,000 people to 2030.
- **Disease burden**: one map per ESPEN disease coloured by the 0-3 endemicity rank, two to a row (non-endemic LGAs shaded pale yellow; LGAs never reported with a known status stay blank, never shaded as 0), the rank distribution per disease, a map of the malaria parasite rate in children aged 2-10 (Malaria Atlas Project, 2025) in five bands from under 15% to 30% and over, with a legend and a note on how it enters the score, and a per-LGA burden table with population, facilities per 10,000 people, the five disease ranks, the malaria parasite rate and clinical cases per 1,000 a year.
- **Access**: maps of the share of people over an hour's walk from a facility (Malaria Atlas Project travel time against WorldPop 2025) and of the 0-2 access risk score (hard to reach on foot, flood-affected in 2024), and a table of LGAs with access risk, hardest to reach first, flood-affected shown as Yes or No. Every map's hover summary also gives walking time, the 2024 flood flag and the score.
- **Facility mix**: maps of secondary and tertiary facilities per 100,000 people and of the share of facilities recorded as not functional (shading only LGAs with at least five facilities of known status, since GRID3 records status only in v3.0 and often as Unknown), facilities by level per state (largest state first), and a point map of every secondary and tertiary facility with coordinates, coloured by level (primary facilities are already summarised in the per-capita maps, and at national scale their points would hide the rest).
- **Outlook**: national facilities per 10,000 people for each WorldPop year to 2030 at today's facility count, LGAs below 2 per 10,000 by year, and the LGAs at or above 2 today that fall below it by 2030.
- **Version drift**: the v2.0-to-v3.0 lineage split, name agreement of carried-over facilities, lineage by state, and the dropped/appeared review queue.
- **Data status**: a live summary of the published register (build time, the GRID3 release and state count for each version, facility totals, the national population and facilities per 10,000 people, hard-to-reach and 2024 flood-affected LGAs, LGAs with a known ESPEN status per disease, lineage totals), read from `register_summary` so it changes with every refresh; then the sources and licences for every source (GRID3, geoBoundaries, ESPEN, which is non-commercial, WorldPop, the Malaria Atlas Project and NASA MODIS), the "missing is not zero" rule, and links to the attribution, data contract and architecture documents on GitHub.

Filters in the left panel: State, Facility status, Priority tier and Hard to reach apply across the dashboard; LGA applies to the LGA profile tab only, and its list narrows to the chosen state. The ranked list's links set the LGA filter through the dashboard URL (`?native_filters=` with the LGA, and `#TAB-profile`), so a profile link can also be shared or bookmarked.

The dashboard has its own look: a deep teal title band (#034149, the darkest fill of the maps), a warm off-white page, and every chart on a plain white card framed by a hairline border, a rule under its title and a faint shadow, so map and chart colours read true; tables have a tinted header row and a row hover. Explanatory notes sit on a pale teal panel with a teal rule, so text never reads as data. That styling is the dashboard's CSS in the bundle, so `make dashboard` applies it. Superset's accent colour (buttons, links, the open tab, focus rings) is set to the same teal by `THEME_OVERRIDES` in `superset/superset_config.py`, which is built into the Superset image: after pulling a change to it, rebuild with `docker compose build superset superset-worker` and restart with `docker compose up -d`. The accent applies across the whole Superset instance, not only this dashboard.

State and facility-status filters apply across the tabs.

Presentation conventions, kept when charts are edited:

- **Colour carries meaning, the same everywhere.** The dashboard's label colours fix it: the endemicity ranks 0-3 run pale yellow to dark red with unknown in grey; lineage and name agreement use the colour-blind-safe Okabe-Ito set (carried over or exact match blue or green, close amber, dropped or divergent vermilion); facility levels run light to dark blue with tertiary in vermilion.
- **Maps frame Nigeria.** Polygon maps keep a saved viewport (Superset 4.1 fits only point layers to their data): zoom 5.0 for a map half the dashboard wide, 4.85 for two thirds, centred on 8.7 E, 9.0 N. Maps sit at most two to a row, so the whole country, Borno included, stays in frame.
- **A measured zero is drawn.** The deck.gl Polygon layer leaves a polygon unfilled when its value is 0, which would make a measured 0 look unmeasured, so every map whose lowest band starts at 0 maps 0 to 0.001 in its metric (`CASE WHEN ... = 0 THEN 0.001 ELSE ... END`); the hover shows 0.001 for those LGAs. Blank always means not measured. A test enforces it.
- **Legends where bands are ranges.** Continuous maps (rates, shares, the score) carry a legend; maps of a 0-3 rank or the 0-2 access score have a colour key in their note instead, because the legend can only label ranges (0 - 1, 1 - 2) and would misstate whole-number values.
- **Bars and lines start at zero.** Bar charts compare lengths, so their value axis starts at 0; a single LGA's trend is a line with exact labels, and its axis starts at 0 too, so a small decline is not drawn as a cliff. Single-series charts use the dashboard teal.
- **Numbers read as numbers.** Counts and populations carry thousands separators, rates two decimal places, shares a percent sign; the headline facility count and the by-state table are exact, not abbreviated, and tables carry no in-cell bars.
- **Ranked lists are sorted by size,** largest first, never alphabetically by default.

To bring it up:

1. Set real credentials first. Copy `.env.example` to `.env` beside the compose file and fill in `SUPERSET_SECRET_KEY`, `SUPERSET_ADMIN_PASSWORD` and `SUPERSET_DB_PASSWORD` (`SUPERSET_ADMIN_USERNAME` defaults to `admin`). The compose file requires all three, so `docker compose up` aborts with a clear message if any is unset rather than booting with a guessable default. The real `.env` is gitignored. **Never commit real credentials.**
2. Start the stack (`docker compose up -d`) and let a refresh publish the register.
3. Load the dashboard: `make dashboard` (or `python scripts/superset_dashboard.py import`). It zips the bundle, copies it into the `superset` container and loads it with Superset's assets importer (`ImportAssetsCommand`), which creates the connection, datasets, charts and dashboard, or on a re-run overwrites every one of them in place by UUID, so upgrading a deployment is re-running `make dashboard` after pulling the new bundle. The import runs as the user named by `SUPERSET_ADMIN_USERNAME`, which must exist. Changes made in the UI to these objects are replaced by the bundle on the next import, so export them first (below) if they should be kept. Then open `http://127.0.0.1:8088` and log in.

To change the dashboard, edit it in the Superset UI, then run `make dashboard-export` (or `python scripts/superset_dashboard.py export`). That exports only this dashboard, selected by UUID so other dashboards on the instance never land in the repo, and rewrites the bundle directory with stable filenames; review the diff and commit it. `tests/test_superset_bundle.py` checks the bundle in CI: every reference resolves, the connection stays read-only, and every column a dataset or chart names exists in the dbt contract, so renaming a mart column fails the tests instead of breaking a chart.

The live summary is a Table chart whose single cell is HTML built in SQL by a calculated column on the `register_summary` dataset, rendered with the table's sanitised HTML option. Superset's Handlebars chart would read more naturally but compiles its template with `eval`, which Superset's own Content Security Policy blocks; loosening the policy for one panel is not worth weakening it for the whole app.

The map charts use the deck.gl Polygon layer, which needs each polygon's coordinate rings rather than a GeoJSON object, so the choropleth dataset adds a calculated `polygon_coordinates` column (`json_extract(geometry_geojson, '$.coordinates')`). The map rows are polygon parts (800 over 774 LGAs), so the map metrics use `MAX`, never `SUM`.

Hovering an LGA on any map shows a one-line summary: LGA and state, facility count with register version and status, facilities per 10,000 people, the five endemicity ranks (`unknown` where no status is known), travel time, the 2024 flood flag and access risk, the malaria parasite rate and clinical cases per 1,000 a year, the secondary and tertiary facility count and the not-functional share, the priority score with its burden, coverage, access and malaria points and its tier, and the original priority flag, followed by the map's own value. The Polygon layer's built-in tooltip prints a row's `name` field as plain text and only renders when the chart has a metric, so the choropleth dataset defines `name` as an alias of the mart's `lga_summary` column, and every map passes it through `js_columns`. The wording itself lives in dbt, under the contract and its tests. This needs no JavaScript controls and stays within Superset's Content Security Policy. `tests/test_superset_bundle.py` fails if a map loses its metric or the hover column.

The deck.gl basemap tiles come from Mapbox. Set `MAPBOX_API_KEY` in `.env` to a Mapbox public token (`pk...`) for the map background; without it the tiles do not load, though the polygons, non-map charts and the data layer are unaffected. The token stays in the gitignored `.env`, never committed.

### Downloads

Download as PDF or image is rendered on the server, so it works on every tab, maps included. The browser's own exporter cannot capture the map tabs: the Mapbox attribution styles embed SVG references that it tries to fetch as URLs, and the export aborts. `superset/superset_config.py` turns on Superset's webdriver downloads instead: the Download menu queues a job, `superset-worker` opens the dashboard as the requesting user in headless Chromium (Playwright, which renders the deck.gl layers), with the same tab and filter state, takes the screenshot and caches it in Redis for a day, and the page downloads it from there. A download takes a few seconds longer than the browser's exporter, mostly waiting for the map tiles to load. The worker loads the Mapbox basemap itself, so it needs the same `MAPBOX_API_KEY` and outbound HTTPS to Mapbox; without them the download shows the maps without their background.

Superset serves the screenshot endpoints only with its `THUMBNAILS` flag on as well, so the dashboard and chart lists can also show card thumbnails, rendered by the same worker.

The Superset image (`superset/Dockerfile`) adds the `duckdb-engine` driver with DuckDB pinned to the version the pipeline writes the register with (`pyproject.toml`), Playwright 1.44.0 with Chromium, Pillow 10.4.0 (which assembles the PDF), the Postgres driver for the metadata database and the config file to the digest-pinned upstream image. That Playwright release pins the same `greenlet` version this Superset release ships, so Superset's own dependencies are unchanged. Chromium and its system libraries take the image from about 1.3 GB to 2.75 GB. `docker compose up -d --build` rebuilds it after an edit to either file.

### Metadata and caching

Superset keeps its own records (users, roles, charts, dashboards, permalinks) in the `superset-db` Postgres service, on the `superset_db` volume. Postgres rather than SQLite because the web server and the download worker both write to it. It is independent of the register and survives a register rebuild; the dashboard itself can always be restored from the bundle with `make dashboard`, so what the volume really holds is users and roles.

A deployment from before Postgres kept those records in SQLite on the `superset_home` volume, which stays mounted and untouched. On the first start with Postgres, the bootstrap creates the admin user again from `.env`; re-run `make dashboard`, then recreate any other users and roles in the UI. Superset has no built-in tool to move records between the two databases.

Chart query results are cached in Redis for up to a day, so repeat views of a chart do not query DuckDB again. Superset's cache key is the query, not the register's contents, so after each publish the refresh job empties that cache (`NFR_SUPERSET_CACHE_REDIS_URL` in the Dagster services names its Redis database, which holds nothing else). The dashboard shows a new register as soon as it is published. If the clear fails, the refresh still succeeds, logs a warning and records `dashboard_cache: not cleared` in the asset metadata; the dashboard then shows the previous figures until the cache expires. Superset opens a new DuckDB connection for every query, so it never keeps reading a register that has been replaced.

### Who can see what

The bundle's connection is exposed to SQL Lab on purpose, read-only; see [ADR 0001](adr/0001-superset-sql-lab-access.md). Access is controlled with Superset roles, not by hiding the connection:

- **Viewers**: the Gamma role plus access to the dashboard's datasets. They see the dashboard and can export chart data, but cannot run SQL.
- **Analysts**: additionally the `sql_lab` role, which lets them query every table in the register (marts, staging, raw), read-only. Grant it only to named people who have accepted the ESPEN non-commercial terms, since SQL Lab makes the full per-LGA endemicity table easy to export.
- **Admins**: manage roles and import the bundle.

Before opening the dashboard to anyone outside the team (self-registration, a public or guest role, embedding), check each of these:

1. Self-registration stays off (`AUTH_USER_REGISTRATION = False`, the default), or new users default to a role without `sql_lab`.
2. Any public or guest role (`PUBLIC_ROLE_LIKE`, guest tokens) is limited to the dashboard's datasets and has no `sql_lab`.
3. The audience may use the ESPEN data under its non-commercial terms; if not, remove the ESPEN charts or datasets for that audience.
4. If that audience should never run SQL, set `expose_in_sqllab: false` in `superset/dashboards/nfr_coverage_burden/databases/NFR_register.yaml` and update ADR 0001 and its test in the same change.

## Not covered here

This is a single-host compose deployment. A cloud/orchestrated deployment
(Kubernetes, a managed Dagster, external run storage in Postgres) is a separate
step; the image is a plain Python/uv image, so it drops into those without
change, but their manifests are out of scope.

## Status

The image and compose file are committed and validated by parse/import; a live
`docker compose up` has not been run in CI (no container runtime there), so the
first real deployment should expect to iterate on host specifics (GDAL runtime
version, volume permissions).
