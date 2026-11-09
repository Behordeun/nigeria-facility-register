"""
The dbt project as Dagster sees it: where it lives, and how its models, seeds,
tests and sources map onto asset keys.

Every dbt model and seed becomes its own asset in the refresh group, and every
dbt test attached to one model becomes an asset check on it. Every dbt source
maps to the one raw_sources asset, because the loader writes all the raw tables
in a single step; the dbt sources are tables in that step's output, not assets
Dagster could materialise separately.

The manifest is not committed. `dagster dev` parses the project on start
(prepare_if_dev), the deployment image parses it at build time, and CI parses
it as part of the dbt build. Code that only needs the translator (the unit
tests, the job selection) does not import the manifest.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dagster import AssetKey
from dagster_dbt import DagsterDbtTranslator, DagsterDbtTranslatorSettings, DbtProject

from nfr.orchestration import config

REFRESH_GROUP = "refresh"
RAW_SOURCES_KEY = AssetKey("raw_sources")

DBT_PROJECT = DbtProject(project_dir=config.DBT_DIR, profiles_dir=config.DBT_DIR)

# The dbt next to the running interpreter, not whichever `dbt` is first on PATH,
# so the build always uses this environment's pinned dbt-core and adapter.
DBT_EXECUTABLE = str(Path(sys.executable).parent / "dbt")


class RegisterTranslator(DagsterDbtTranslator):
    """Map dbt sources onto raw_sources and keep every dbt node in the refresh group."""

    def get_asset_key(self, dbt_resource_props: Mapping[str, Any]) -> AssetKey:
        if dbt_resource_props["resource_type"] == "source":
            return RAW_SOURCES_KEY
        return super().get_asset_key(dbt_resource_props)

    def get_group_name(self, dbt_resource_props: Mapping[str, Any]) -> str:
        return REFRESH_GROUP


# Several dbt sources share the raw_sources key, which dagster-dbt refuses
# unless duplicate source keys are allowed explicitly.
TRANSLATOR = RegisterTranslator(
    settings=DagsterDbtTranslatorSettings(enable_duplicate_source_asset_keys=True)
)


@contextmanager
def dbt_database(path: Path) -> Iterator[None]:
    """Point dbt invocations in this process at the DuckDB file `path` for the block.

    The dbt profile reads the database path from config.DBT_DB_ENV, and
    dagster-dbt passes each dbt invocation a copy of os.environ with no per-call
    override, so the variable is set here and restored afterwards (removed if it
    was unset). Each refresh run executes in its own process and the
    concurrency key admits one refresh at a time, so no other dbt invocation
    shares the setting.
    """
    previous = os.environ.get(config.DBT_DB_ENV)
    os.environ[config.DBT_DB_ENV] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(config.DBT_DB_ENV, None)
        else:
            os.environ[config.DBT_DB_ENV] = previous
