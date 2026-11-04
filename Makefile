# Pipeline entry points. Run `make help` for the list.

PY ?= .venv/bin/python
DB ?= build/nfr.duckdb

.PHONY: help setup fetch load build test lint clean

help:
	@echo "setup   install the package and dev deps into .venv"
	@echo "fetch   check source inputs are present (download manually from HDX)"
	@echo "load    load source inputs into DuckDB (the raw schema)"
	@echo "build   run the dbt staging + marts models"
	@echo "test    unit tests + dbt tests"
	@echo "lint    ruff"
	@echo "clean   remove build artifacts"

setup:
	python3 -m venv .venv
	$(PY) -m pip install -e ".[dev]"

fetch:
	$(PY) scripts/fetch_sources.py --verify

load:
	$(PY) -m nfr.load_sources --db $(DB)

build: load
	cd dbt && ../$(PY) -m dbt build --profiles-dir .

test:
	$(PY) -m pytest -q
	cd dbt && ../$(PY) -m dbt test --profiles-dir . || true

lint:
	$(PY) -m ruff check src tests

clean:
	rm -rf build dbt/target dbt/logs
