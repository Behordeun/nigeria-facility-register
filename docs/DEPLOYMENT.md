# Deployment

The dev and CI loops do not need a container: `uv` plus the committed lockfile
reproduce the environment, and `make sample` / `make build` run the transform
directly. This document is for an **always-on deployment**, where the Dagster
schedule and sensor must keep running so the register refreshes on its own.

## What runs

Three services (`docker-compose.yml`); the first two share the project image
(`Dockerfile`):

- **webserver** — the Dagster UI, on `127.0.0.1:3000`.
- **daemon** — runs the weekly schedule and the new-vintage sensor, and launches
  the `refresh_register` job.
- **superset** — Apache Superset (official image), on `127.0.0.1:8088`, reading
  the register read-only for dashboards. See the Superset section below.

Both share a `DAGSTER_HOME` volume (one instance, one run store, one set of
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
# build the image and start both services
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
- `data/raw` is mounted read-only, so a container cannot alter the operator's
  placed inputs.

## Superset (dashboards)

The compose stack includes an Apache Superset service for the dashboards: the
coverage x burden choropleth (from `lga_coverage_choropleth`, which carries each
LGA's boundary polygon as GeoJSON) and the version-drift breakdown (from
`lga_version_drift`). It reads the live register **read-only** (the `build`
volume mounted at `/app/register`), so a dashboard query can never alter the
published data.

Bring it up with the rest of the stack (`docker compose up -d`), then at
`http://127.0.0.1:8088`:

1. Set real credentials first. Put a `.env` beside the compose file with
   `SUPERSET_SECRET_KEY`, `SUPERSET_ADMIN_USERNAME`, `SUPERSET_ADMIN_PASSWORD`;
   the committed defaults are placeholders that only let a local trial boot.
   **Never commit a real secret key.**
2. Add the database connection (Settings -> Database Connections -> + Database,
   "Other"): SQLAlchemy URI `duckdb:////app/register/nfr.duckdb?access_mode=read_only`.
   The `read_only` access mode matches the read-only mount and lets Superset
   open the file even while the daemon holds it.
3. Add the two datasets from that connection: `lga_coverage_choropleth` and
   `lga_version_drift`. The choropleth's `geometry_geojson` column feeds a
   deck.gl Polygon / Geojson chart; colour by `facilities`,
   `sth_high_share_pct_state`, or `priority_high_burden_thin_coverage`.

Superset keeps its own metadata (users, charts, dashboards) in the
`superset_home` volume; it is independent of the register and survives a
register rebuild.

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
