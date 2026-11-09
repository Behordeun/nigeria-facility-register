"""
The refresh job and the upstream-release check job.
"""
from __future__ import annotations

from dagster import AssetSelection, define_asset_job

from nfr.orchestration.dbt_project import REFRESH_GROUP

# Tag the job's runs with a concurrency key, so a scheduled, sensor, and manual
# launch do not overlap and race on the shared DuckDB file and dbt output dirs.
# The run coordinator serialises runs that share this key (configure the limit
# to 1 for the "refresh_register" key in the instance's run-concurrency settings;
# the tag is what makes that limit apply to every launch path).
refresh_job = define_asset_job(
    "refresh_register",
    # The refresh group (raw_sources, every dbt model and seed, and
    # published_register, with their checks), not "*": the upstream release
    # watch (upstream_check_job) is a separate job and must never run inside, or
    # hold up, a refresh. Selected by group name so this module does not import
    # the dbt assets, which need the parsed dbt manifest.
    selection=AssetSelection.groups(REFRESH_GROUP),
    tags={"dagster/concurrency_key": "refresh_register"},
)


upstream_check_job = define_asset_job(
    "check_upstream_releases",
    selection=AssetSelection.assets("upstream_releases"),
)
