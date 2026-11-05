"""
Dagster definitions for the facility-register refresh.

The pipeline is two moving parts: load the source files into DuckDB, then run
the dbt build (staging and marts). This wraps both as Dagster assets so a run is
observable and re-runnable, adds a weekly schedule, and adds a sensor that fires
only when a new GRID3 vintage actually lands in data/raw/.

One honest limit: GRID3 is published behind an AWS WAF challenge on HDX that a
scripted download cannot clear, so the raw geopackage fetch stays a manual step
(scripts/fetch_sources.py prints the links). The sensor watches for the file to
appear rather than pretending to pull it. Everything after the file lands is
automated.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from dagster import (
    Definitions,
    ScheduleDefinition,
    SensorEvaluationContext,
    SensorResult,
    asset,
    define_asset_job,
    sensor,
)

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "data" / "raw"
DB = ROOT / "build" / "nfr.duckdb"
DBT_DIR = ROOT / "dbt"
RAW_GPKGS = ["nga_health_facilities_v2_0.gpkg", "grid3_nga_health_facilities_v3_0.gpkg"]


@asset
def raw_sources(context) -> None:
    """Load the source files into DuckDB as the raw schema."""
    missing = [n for n in RAW_GPKGS if not (RAW / n).exists()]
    if missing:
        raise RuntimeError(
            f"missing raw inputs {missing}; fetch them with scripts/fetch_sources.py "
            "(HDX serves them behind a WAF, so the download is manual)"
        )
    subprocess.run(
        ["python", "-m", "nfr.load_sources", "--db", str(DB)],
        cwd=str(ROOT), check=True,
    )
    context.add_output_metadata({"db": str(DB)})


@asset(deps=[raw_sources])
def dbt_models(context) -> None:
    """Run the dbt staging and marts build, then the dbt tests."""
    subprocess.run(["dbt", "build", "--profiles-dir", "."], cwd=str(DBT_DIR), check=True)


refresh_job = define_asset_job("refresh_register", selection="*")

# Weekly, not daily: GRID3 releases are infrequent, so a weekly rebuild keeps the
# marts current without spinning for no reason.
weekly_refresh = ScheduleDefinition(
    job=refresh_job,
    cron_schedule="0 6 * * 1",  # Monday 06:00
    name="weekly_refresh",
)


def fingerprint(raw_dir: Path = RAW, names: list[str] | None = None) -> str:
    """Hash the raw geopackages so the sensor fires only on a genuine change.

    Pure and path-injectable so it can be tested without a Dagster context.
    An absent file contributes nothing, so the fingerprint changes when a file
    appears, grows, or is replaced with a newer vintage.
    """
    h = hashlib.sha256()
    for n in names or RAW_GPKGS:
        p = raw_dir / n
        if p.exists():
            h.update(n.encode())
            h.update(str(p.stat().st_size).encode())
            h.update(str(int(p.stat().st_mtime)).encode())
    return h.hexdigest()[:16]


@sensor(job=refresh_job, minimum_interval_seconds=3600)
def new_vintage_sensor(context: SensorEvaluationContext) -> SensorResult:
    """Fire the refresh when a new GRID3 vintage appears in data/raw/.

    Uses the file fingerprint as the cursor, so an unchanged data/raw/ costs
    nothing and a newly dropped geopackage triggers exactly one rebuild.
    """
    fp = fingerprint()
    if fp == (context.cursor or ""):
        return SensorResult(skip_reason="no new GRID3 vintage in data/raw/")
    from dagster import RunRequest
    return SensorResult(run_requests=[RunRequest(run_key=fp)], cursor=fp)


defs = Definitions(
    assets=[raw_sources, dbt_models],
    jobs=[refresh_job],
    schedules=[weekly_refresh],
    sensors=[new_vintage_sensor],
)
