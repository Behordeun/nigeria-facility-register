# Pipeline entry points. Run `make help` for the list.

PY ?= .venv/bin/python
DB ?= build/nfr.duckdb

.PHONY: help setup fetch load build sample test dagster dashboard dashboard-export lint clean

help:
	@echo "setup   install the package and dev deps into .venv"
	@echo "fetch   check source inputs are present (download manually from HDX)"
	@echo "load    load source inputs into DuckDB (the raw schema)"
	@echo "build   run the dbt staging + marts models (needs the real inputs in data/raw/)"
	@echo "sample  run the whole transform on the committed sample fixture (no download)"
	@echo "test    unit tests + dbt tests"
	@echo "dagster launch the Dagster dev UI for the refresh pipeline"
	@echo "dashboard        import the committed Superset dashboard into the running compose stack"
	@echo "dashboard-export export the dashboard from the running Superset back into superset/"
	@echo "lint    ruff"
	@echo "clean   remove build artifacts"

setup:
	python3 -m venv .venv
	$(PY) -m pip install -e ".[dev]"

fetch:
	$(PY) scripts/fetch_sources.py --verify

load:
	$(PY) -m nfr.load_sources --db $(DB)

# -m dbt.cli.main, not -m dbt: dbt ships no dbt.__main__, so -m dbt fails with
# "No module named dbt.__main__".
build: load
	cd dbt && ../$(PY) -m dbt.cli.main build --profiles-dir .

# End-to-end on the committed two-state sample fixture, no external download.
# Loads data/sample and builds with fixture_build so the fixture-only coverage
# tests run. This is the same path CI uses to prove the transform on every PR.
sample:
	$(PY) -m nfr.load_sources --db $(DB) --raw data/sample
	cd dbt && ../$(PY) -m dbt.cli.main build --profiles-dir . --vars 'fixture_build: true'

test:
	$(PY) -m pytest -q
	cd dbt && ../$(PY) -m dbt.cli.main test --profiles-dir . || true

# Superset dashboard bundle (superset/dashboards/). Needs `docker compose up -d`.
dashboard:
	$(PY) scripts/superset_dashboard.py import

dashboard-export:
	$(PY) scripts/superset_dashboard.py export

lint:
	$(PY) -m ruff check src tests

dagster:
	$(PY) -m pip install -e ".[dev,orchestration]"
	DAGSTER_HOME=$(CURDIR) $(PY) -m dagster dev -m nfr.orchestration.definitions

clean:
	rm -rf build dbt/target dbt/logs
