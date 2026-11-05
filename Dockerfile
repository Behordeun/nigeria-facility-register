# Dagster deployment image: the webserver and the daemon run from this same
# image (see docker-compose.yml), sharing one DAGSTER_HOME. The dev and CI loops
# do NOT use this image; they use uv + the lockfile directly. This exists only
# for an always-on deployment where the schedule and sensor must keep running.
#
# Built on the official uv image with Python 3.12 (matching CI's
# `uv sync --frozen`), so the container resolves the exact pinned versions from
# uv.lock and cannot drift from what CI tested.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

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
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra orchestration

# Then the project source. Copied after the dependency layer so a code change
# does not invalidate the dependency install.
COPY . .
RUN uv sync --frozen --extra orchestration

# The instance config seed; the compose daemon/webserver share a DAGSTER_HOME
# volume, and the entrypoint copies this in if the volume is empty.
RUN mkdir -p "$DAGSTER_HOME" && cp dagster.yaml "$DAGSTER_HOME/dagster.yaml"

# No CMD: docker-compose sets the command per service (webserver vs daemon), so
# this image is a single artifact that runs either role.
