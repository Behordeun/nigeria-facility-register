# Dagster deployment image: the webserver and the daemon run from this same
# image (see docker-compose.yml), sharing one DAGSTER_HOME. The dev and CI loops
# do NOT use this image; they use uv + the lockfile directly. This exists only
# for an always-on deployment where the schedule and sensor must keep running.
#
# Built on the official uv image with Python 3.12 (matching CI's
# `uv sync --frozen`), so the container resolves the exact pinned versions from
# uv.lock and cannot drift from what CI tested. Pinned to a SHA-256 digest, not
# just the floating python3.12-bookworm-slim tag, so a rebuild is byte-for-byte
# reproducible and a mutated tag cannot swap in a different base. Refresh the
# digest deliberately: docker pull ghcr.io/astral-sh/uv:python3.12-bookworm-slim
# && docker inspect --format='{{index .RepoDigests 0}}' that image.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

# dbt-duckdb shells out to no system tools, but geopandas/pyogrio need the GDAL
# runtime libraries; install them before the Python deps.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgdal32 \
    && rm -rf /var/lib/apt/lists/*

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # The instance config (dagster.yaml) lives here; the compose volume mounts a
    # writable DAGSTER_HOME over it at run time, seeded from the repo copy.
    DAGSTER_HOME=/opt/dagster/home \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first, from the lockfile only, so this layer caches and
# is reinstalled only when the lock or project metadata changes. --frozen fails
# if the lock is out of date, matching CI. The orchestration extra brings in
# dagster + the webserver; dev is not installed in the deployment image.
#
# Both uv steps mount a persistent BuildKit cache at uv's cache directory. That
# cache lives outside the image layers, so when a code or lockfile change
# invalidates these layers, wheels downloaded or built earlier are reused rather
# than fetched again. This matters most for dbt-core-experimental-parser, which
# ships only an sdist whose build step downloads its platform wheel from GitHub
# releases; with the cache, that download happens once per machine and parser
# version. UV_LINK_MODE=copy (above) copies packages out of the mounted cache
# into the venv, since hard links cannot cross into a cache mount.
#
# `--no-cache` builds start from an empty cache mount, so the dependency step
# also retries up to three times, 30s apart, to ride out a short upstream
# download outage. Packages fetched by a failed attempt stay in the mount, so a
# retry fetches only what is still missing.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    for attempt in 1 2 3; do \
        uv sync --frozen --no-install-project --extra orchestration && exit 0; \
        echo "uv sync failed (attempt $attempt of 3)" >&2; \
        [ "$attempt" = 3 ] || sleep 30; \
    done; \
    exit 1

# Then the project source. Copied after the dependency layer so a code change
# does not invalidate the dependency install.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra orchestration

# The instance config seed; the compose daemon/webserver share a DAGSTER_HOME
# volume, and the entrypoint copies this in if the volume is empty.
RUN mkdir -p "$DAGSTER_HOME" && cp dagster.yaml "$DAGSTER_HOME/dagster.yaml"

# No CMD: docker-compose sets the command per service (webserver vs daemon), so
# this image is a single artifact that runs either role.
