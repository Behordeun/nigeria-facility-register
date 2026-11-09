"""
Dagster definitions for the facility-register refresh.

The pipeline is two moving parts: load the source files into DuckDB, then run
the dbt build (staging and marts). This wraps both as Dagster assets so a run is
observable and re-runnable, adds a weekly schedule, and adds a sensor that fires
only when the inputs change: a new GRID3 vintage in data/raw/, an edited frame
or derived table, a changed dbt model, or a new population year.

One honest limit: GRID3 is published behind an AWS WAF challenge on HDX that a
scripted download cannot clear, so the raw geopackage fetch stays a manual step
(scripts/fetch_sources.py prints the links). The sensor watches for the file to
appear rather than pretending to pull it. Everything after the file lands is
automated.
"""
from __future__ import annotations

from dagster import Definitions

from nfr.orchestration.assets import dbt_models, raw_sources, upstream_releases
from nfr.orchestration.checks import (
    carryover_gate_check,
    carryover_rate_check,
    lga_resolve_rate_check,
    no_unreviewed_upstream_release,
)
from nfr.orchestration.jobs import refresh_job, upstream_check_job
from nfr.orchestration.schedules import monthly_upstream_check, weekly_refresh
from nfr.orchestration.sensors import new_vintage_sensor, refresh_failure_sensor

defs = Definitions(
    assets=[raw_sources, dbt_models, upstream_releases],
    asset_checks=[
        lga_resolve_rate_check, carryover_rate_check, carryover_gate_check, no_unreviewed_upstream_release,
    ],
    jobs=[refresh_job, upstream_check_job],
    schedules=[weekly_refresh, monthly_upstream_check],
    sensors=[new_vintage_sensor, refresh_failure_sensor],
)
