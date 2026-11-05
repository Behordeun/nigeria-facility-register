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
import sys
from pathlib import Path

from dagster import (
    DefaultScheduleStatus,
    DefaultSensorStatus,
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
    # sys.executable, not a bare "python", so the subprocess runs the same
    # interpreter (and virtualenv) as the Dagster process, with the project
    # dependencies available.
    subprocess.run(
        [sys.executable, "-m", "nfr.load_sources", "--db", str(DB)],
        cwd=str(ROOT), check=True,
    )
    context.add_output_metadata({"db": str(DB)})


@asset(deps=[raw_sources])
def dbt_models(context) -> None:
    """Run the dbt staging and marts build, then the dbt tests."""
    # Run dbt as a module of this interpreter, so it resolves to the dbt beside
    # the virtualenv's Python rather than whatever is on the system PATH.
    subprocess.run(
        [sys.executable, "-m", "dbt", "build", "--profiles-dir", "."],
        cwd=str(DBT_DIR), check=True,
    )


# Cap the job at one active run, so a scheduled, sensor, and manual launch cannot
# overlap and race on the same DuckDB file.
refresh_job = define_asset_job(
    "refresh_register",
    selection="*",
    config={"execution": {"config": {"multiprocess": {"max_concurrent": 1}}}},
)

# Weekly, not daily: GRID3 releases are infrequent, so a weekly rebuild keeps the
# marts current without spinning for no reason. default_status RUNNING so the
# schedule is active on load without a manual toggle.
weekly_refresh = ScheduleDefinition(
    job=refresh_job,
    cron_schedule="0 6 * * 1",  # Monday 06:00
    name="weekly_refresh",
    default_status=DefaultScheduleStatus.RUNNING,
)


def fingerprint(raw_dir: Path = RAW, names: list[str] | None = None) -> str:
    """Hash the raw geopackage CONTENTS so the sensor fires only on a genuine
    change.

    Hashes each file's bytes in chunks, not its size and mtime: a replacement
    vintage of identical byte length with a preserved whole-second mtime (a cp
    --preserve, an rsync --times, filesystem rounding) would otherwise produce
    the same fingerprint and the rebuild would be silently skipped. Pure and
    path-injectable so it can be tested without a Dagster context; an absent
    file contributes nothing.
    """
    h = hashlib.sha256()
    for n in names or RAW_GPKGS:
        p = raw_dir / n
        if p.exists():
            h.update(n.encode())
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
    return h.hexdigest()[:16]


@sensor(
    job=refresh_job,
    minimum_interval_seconds=3600,
    default_status=DefaultSensorStatus.RUNNING,
)
def new_vintage_sensor(context: SensorEvaluationContext) -> SensorResult:
    """Fire the refresh when a new GRID3 vintage appears in data/raw/.

    Requires every raw input to be present before requesting a run, so a
    partially-populated data/raw/ never triggers a build that would fail on the
    missing file. Uses the content fingerprint as the cursor, so an unchanged
    data/raw/ costs nothing and a newly dropped geopackage triggers exactly one
    rebuild. The cursor is advanced only after the run is requested, so a tick
    that cannot run (missing input) leaves the cursor untouched and retries on
    the next tick without needing a new fingerprint.
    """
    missing = [n for n in RAW_GPKGS if not (RAW / n).is_file()]
    if missing:
        return SensorResult(skip_reason=f"waiting for raw inputs: {', '.join(missing)}")
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
