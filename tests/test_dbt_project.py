"""Tests for how the dbt project maps onto Dagster assets and which database dbt builds into.

These need no parsed manifest: the translator is exercised with the resource
properties dbt writes for each node, and the database helper only touches the
environment.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from dagster import AssetKey

from nfr.orchestration import config
from nfr.orchestration.dbt_project import (
    RAW_SOURCES_KEY,
    REFRESH_GROUP,
    TRANSLATOR,
    dbt_database,
)


def _node(resource_type: str, name: str, source_name: str | None = None) -> dict:
    props = {"resource_type": resource_type, "name": name, "fqn": ["nigeria_facility_register", name],
             "config": {}, "meta": {}}
    if source_name is not None:
        props["source_name"] = source_name
    return props


@pytest.mark.parametrize("table", ["grid3_v2", "grid3_v3", "frame_master_index", "births_lga"])
def test_every_dbt_source_maps_to_the_one_raw_sources_asset(table):
    key = TRANSLATOR.get_asset_key(_node("source", table, source_name="raw"))
    assert key == RAW_SOURCES_KEY, f"source raw.{table} must depend on the loader step, got {key}"


@pytest.mark.parametrize("resource_type,name", [("model", "lga_coverage_burden"), ("seed", "ntd_endemicity_rank")])
def test_models_and_seeds_keep_their_own_asset_key(resource_type, name):
    assert TRANSLATOR.get_asset_key(_node(resource_type, name)) == AssetKey(name), \
        f"{resource_type} {name} must be its own asset, keyed by its name"


def test_every_dbt_node_joins_the_refresh_group():
    for node in (_node("model", "stg_version_lineage"), _node("seed", "ntd_endemicity_rank")):
        assert TRANSLATOR.get_group_name(node) == REFRESH_GROUP, \
            "the refresh job selects by group, so a dbt node outside it would never be built"


def test_duplicate_source_keys_are_allowed():
    assert TRANSLATOR.settings.enable_duplicate_source_asset_keys, \
        "several dbt sources share raw_sources, which dagster-dbt rejects without this setting"


def test_dbt_database_sets_the_profile_path_for_the_block_then_removes_it(monkeypatch, tmp_path):
    monkeypatch.delenv(config.DBT_DB_ENV, raising=False)
    staged = tmp_path / "nfr_run.duckdb"
    with dbt_database(staged):
        assert os.environ[config.DBT_DB_ENV] == str(staged), "dbt must build into the staged file"
    assert config.DBT_DB_ENV not in os.environ, "an unset variable must be unset again afterwards"


def test_dbt_database_restores_a_previous_path_even_when_the_build_fails(monkeypatch, tmp_path):
    monkeypatch.setenv(config.DBT_DB_ENV, "build/scratch.duckdb")
    with pytest.raises(RuntimeError), dbt_database(Path(tmp_path / "nfr_run.duckdb")):
        raise RuntimeError("dbt test failed")
    assert os.environ[config.DBT_DB_ENV] == "build/scratch.duckdb", \
        "a failed build must not leave the process pointing at the discarded side file"
