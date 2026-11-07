# Superset image for the NFR stack: the pinned upstream image plus what the
# dashboard needs at run time, built once so every container starts the same.
#
#   - duckdb-engine: the duckdb:// SQLAlchemy driver for the register.
#   - playwright + Chromium: the headless browser the Celery worker uses to
#     render dashboard downloads (PDF and image) on the server. Playwright, not
#     Selenium, because it renders the deck.gl map layers. 1.44.0 pins
#     greenlet 3.0.3, the version this Superset release already ships, so the
#     install leaves Superset's own dependencies unchanged.
#   - Pillow: turns the screenshot into the PDF download (Superset's optional
#     "thumbnails" extra, which allows Pillow 10.x).
#   - superset_config.py: on PYTHONPATH (/app/pythonpath), which Superset loads
#     automatically.
#
# Chromium goes to /opt/ms-playwright rather than the superset user's home,
# because that home (/app/superset_home) is a mounted volume that would hide it.
FROM apache/superset:4.1.1@sha256:1d1fdaaeb19ce9cdba71620ee1cc6117d73813b2f3b422ce5a1bf752c247b7c0

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

USER root
RUN pip install --no-cache-dir duckdb-engine==0.17.0 playwright==1.44.0 Pillow==10.4.0 \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY superset_config.py /app/pythonpath/superset_config.py

USER superset
