"""Superset settings for the NFR stack.

Superset imports this module after its own defaults, so only what differs from
the defaults is set here.

Dashboard downloads (PDF and image) are rendered on the server. The browser's
own exporter cannot capture the map tabs: the Mapbox attribution styles embed
SVG references that it tries to fetch as URLs, and the export aborts. With the
flags below, the Download menu instead asks the Celery worker to open the
dashboard in headless Chromium (Playwright, which renders deck.gl), screenshot
it, and cache the image in Redis; the page then fetches it from there.
"""

import os

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

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

    broker_url = f"{REDIS_URL}/0"
    result_backend = f"{REDIS_URL}/0"
    imports = ("superset.tasks.thumbnails",)
    worker_prefetch_multiplier = 1
    task_acks_late = False
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

# The worker reaches the web server over the compose network, not the host port.
WEBDRIVER_BASEURL = os.environ.get("SUPERSET_WEBDRIVER_BASEURL", "http://superset:8088/")

# Maps can take a while to draw their tiles; wait for the page to go quiet
# before the screenshot, and give the whole render more room than the default.
SCREENSHOT_PLAYWRIGHT_WAIT_EVENT = "networkidle"
SCREENSHOT_PLAYWRIGHT_DEFAULT_TIMEOUT = 90_000
