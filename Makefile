# Pipeline entry points. Run `make help` for the list.

PY ?= .venv/bin/python
# Local builds go to a scratch file, never build/nfr.duckdb: that path is the
# published register the Dagster refresh promotes onto and Superset reads.
DB ?= build/scratch.duckdb
# dbt reads its database path from NFR_DUCKDB_PATH (see dbt/profiles.yml).
DBT = cd dbt && NFR_DUCKDB_PATH=$(CURDIR)/$(DB) ../$(PY) -m dbt.cli.main

.PHONY: help setup fetch load build sample test dagster dashboard dashboard-export public-viewer registers restore release-hold lint clean

help:
	@echo "setup   install the locked package, dev and orchestration deps into .venv (needs uv)"
	@echo "fetch   check source inputs are present (download manually from HDX)"
	@echo "load    load source inputs into DuckDB (the raw schema)"
	@echo "build   run the dbt staging + marts models into $(DB) (needs the real inputs in data/raw/)"
	@echo "sample  run the whole transform on the committed sample fixture (no download)"
	@echo "test    unit tests + the sample transform and its dbt tests (what CI runs)"
	@echo "dagster launch the Dagster dev UI for the refresh pipeline"
	@echo "registers        list the live register, the rollback hold and the archived registers"
	@echo "restore          put an archived register back: make restore REGISTER=<name>"
	@echo "release-hold     let a vintage held after a rollback be published again"
	@echo "dashboard        import the committed Superset dashboard into the running compose stack"
	@echo "dashboard-export export the dashboard from the running Superset back into superset/"
	@echo "public-viewer    create or update the view-only public-viewer Superset account (after make dashboard)"
	@echo "lint    ruff"
	@echo "clean   remove build artifacts"

# The same frozen lockfile CI installs from, so local and CI versions match.
setup:
	uv sync --frozen --extra dev --extra orchestration

fetch:
	$(PY) scripts/fetch_sources.py --verify

load:
	$(PY) -m nfr.load_sources --db $(DB)

# -m dbt.cli.main, not -m dbt: dbt ships no dbt.__main__, so -m dbt fails with
# "No module named dbt.__main__".
build: load
	$(DBT) build --profiles-dir .

# End-to-end on the committed two-state sample fixture, no external download.
# Loads data/sample and builds with fixture_build so the fixture-only coverage
# tests run. This is the same path CI uses to prove the transform on every PR.
sample:
	$(PY) -m nfr.load_sources --db $(DB) --raw data/sample
	$(DBT) build --profiles-dir . --vars 'fixture_build: true'

# What CI runs: the unit tests, then the whole transform and every dbt test on
# the committed sample. Any failure fails the target. (`make build` already
# runs the dbt tests on a real build.)
test:
	$(PY) -m pytest -q
	$(MAKE) sample

# Rollback of the published register (build/nfr.duckdb, written by the Dagster
# refresh). In the compose deployment run the script inside the daemon container
# instead: docker compose exec daemon uv run python scripts/restore_register.py --list
registers:
	$(PY) scripts/restore_register.py --list

restore:
	@test -n "$(REGISTER)" || { echo "usage: make restore REGISTER=<archived file name>"; exit 2; }
	$(PY) scripts/restore_register.py --restore "$(REGISTER)"

release-hold:
	$(PY) scripts/restore_register.py --release-hold

# Superset dashboard bundle (superset/dashboards/). Needs `docker compose up -d`.
dashboard:
	$(PY) scripts/superset_dashboard.py import

dashboard-export:
	$(PY) scripts/superset_dashboard.py export

public-viewer:
	$(PY) scripts/superset_public_viewer.py

lint:
	$(PY) -m ruff check src tests

dagster:
	DAGSTER_HOME=$(CURDIR) $(PY) -m dagster dev -m nfr.orchestration.definitions

# Leaves build/nfr.duckdb alone: that is the published register, not an artifact.
clean:
	rm -rf $(DB) build/staging build/snapshots dbt/target dbt/logs
