"""
The refresh job and the upstream-release check job.
"""
from __future__ import annotations

from dagster import AssetSelection, define_asset_job

# Tag the job's runs with a concurrency key, so a scheduled, sensor, and manual
# launch do not overlap and race on the shared DuckDB file and dbt output dirs.
# The run coordinator serialises runs that share this key (configure the limit
# to 1 for the "refresh_register" key in the instance's run-concurrency settings;
# the tag is what makes that limit apply to every launch path).
refresh_job = define_asset_job(
    "refresh_register",
    # The two refresh assets (and their checks) by name, not "*": the upstream
    # release watch (upstream_check_job) is a separate job and must never run inside, or hold
    # up, a refresh.
    selection=AssetSelection.assets("raw_sources", "dbt_models"),
    tags={"dagster/concurrency_key": "refresh_register"},
)


upstream_check_job = define_asset_job(
    "check_upstream_releases",
    selection=AssetSelection.assets("upstream_releases"),
)
