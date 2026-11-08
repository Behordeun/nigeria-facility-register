"""Superset settings for the NFR stack.

Superset imports this module after its own defaults, so only what differs from
the defaults is set here.

Dashboard downloads (PDF and image) are rendered on the server. The browser's
own exporter cannot capture the map tabs: the Mapbox attribution styles embed
SVG references that it tries to fetch as URLs, and the export aborts. With the
flags below, the Download menu instead asks the Celery worker to open the
dashboard in headless Chromium (Playwright, which renders deck.gl), screenshot
it, and cache the image in Redis; the page then fetches it from there.

Superset's own records (users, roles, charts, dashboards, permalinks) live in
Postgres, which the web server and the worker share safely. The register itself
stays a DuckDB file that Superset only reads.

Chart query results are cached in Redis. The refresh job clears that cache right
after it publishes a new register (NFR_SUPERSET_CACHE_REDIS_URL in the Dagster
services), so a refresh shows at once; the timeout below only bounds how long a
result can outlive a missed clear.
"""

import os

from sqlalchemy.engine import URL

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

# Built with URL.create so a password with special characters is escaped, not
# misread as part of the URL. The password has no default: the compose file
# requires it.
SQLALCHEMY_DATABASE_URI = URL.create(
    "postgresql+psycopg2",
    username=os.environ.get("SUPERSET_DB_USER", "superset"),
    password=os.environ["SUPERSET_DB_PASSWORD"],
    host=os.environ.get("SUPERSET_DB_HOST", "superset-db"),
    port=5432,
    database=os.environ.get("SUPERSET_DB_NAME", "superset"),
).render_as_string(hide_password=False)

# Superset serves the screenshot endpoints only when THUMBNAILS is on as well.
# THUMBNAILS also lets the dashboard and chart lists show card thumbnails,
# rendered by the same worker.
FEATURE_FLAGS = {
    "THUMBNAILS": True,
    "ENABLE_DASHBOARD_SCREENSHOT_ENDPOINTS": True,
    "ENABLE_DASHBOARD_DOWNLOAD_WEBDRIVER_SCREENSHOT": True,
    "PLAYWRIGHT_REPORTS_AND_THUMBNAILS": True,
}


class CeleryConfig:  # pylint: disable=too-few-public-methods
    """Celery broker and result store for the screenshot worker."""

    # Queue (0), screenshots (1), task results (2) and chart results (3) each
    # get their own Redis database.
    broker_url = f"{REDIS_URL}/0"
    result_backend = f"{REDIS_URL}/2"
    imports = ("superset.tasks.thumbnails",)
    worker_prefetch_multiplier = 1
    # Acknowledge a screenshot job only once it finishes, so a worker that dies
    # mid-render hands the job back to the queue instead of dropping it.
    task_acks_late = True
    # The worker may start before Redis accepts connections; keep retrying.
    broker_connection_retry_on_startup = True


CELERY_CONFIG = CeleryConfig

# Rendered screenshots live here until the page downloads them. A day is ample;
# the cache key changes whenever the dashboard or its filter state changes.
THUMBNAIL_CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 86400,
    "CACHE_KEY_PREFIX": "nfr_screenshot_",
    "CACHE_REDIS_URL": f"{REDIS_URL}/1",
}

# Chart query results. Database 3 holds nothing else, because the refresh job
# empties it whole after each publish.
DATA_CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 86400,
    "CACHE_KEY_PREFIX": "nfr_data_",
    "CACHE_REDIS_URL": f"{REDIS_URL}/3",
}

# The worker reaches the web server over the compose network, not the host port.
WEBDRIVER_BASEURL = os.environ.get("SUPERSET_WEBDRIVER_BASEURL", "http://superset:8088/")

# Maps can take a while to draw their tiles; wait for the page to go quiet
# before the screenshot, and give the whole render more room than the default.
SCREENSHOT_PLAYWRIGHT_WAIT_EVENT = "networkidle"
SCREENSHOT_PLAYWRIGHT_DEFAULT_TIMEOUT = 90_000
