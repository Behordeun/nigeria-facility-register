# Deployment

The dev and CI loops do not need a container: `uv` plus the committed lockfile
reproduce the environment, and `make sample` / `make build` run the transform
directly. This document is for an **always-on deployment**, where the Dagster
schedule and sensor must keep running so the register refreshes on its own.

## What runs

Two long-lived processes from one image (`Dockerfile`), wired up by
`docker-compose.yml`:

- **webserver** — the Dagster UI, on `127.0.0.1:3000`.
- **daemon** — runs the weekly schedule and the new-vintage sensor, and launches
  the `refresh_register` job.

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
