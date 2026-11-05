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
import time
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


# Tag the job's runs with a concurrency key, so a scheduled, sensor, and manual
# launch do not overlap and race on the shared DuckDB file and dbt output dirs.
# The run coordinator serialises runs that share this key (configure the limit
# to 1 for the "refresh_register" key in the instance's run-concurrency settings;
# the tag is what makes that limit apply to every launch path).
refresh_job = define_asset_job(
    "refresh_register",
    selection="*",
    tags={"dagster/concurrency_key": "refresh_register"},
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
    # Confine resolution to the directory being hashed: a listed name may not
    # escape raw_dir via a symlink or a .. segment. This stops the sensor from
    # reading bytes outside the watched directory (in production raw_dir is RAW,
    # inside the project); a caller that passes its own raw_dir, such as a test,
    # is still confined to that directory.
    base = raw_dir.resolve()
    h = hashlib.sha256()
    for n in names or RAW_GPKGS:
        p = raw_dir / n
        if not p.exists():
            continue
        real = p.resolve()
        if base != real and base not in real.parents:
            raise ValueError(f"raw input {real} escapes the watched directory {base}")
        h.update(n.encode())
        with real.open("rb") as f:
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

    Requires every raw input to be present and settled before requesting a run,
    uses a content fingerprint so an unchanged data/raw/ is a no-op, and keys
    each run so a failed refresh or a restored earlier vintage is not
    deduplicated away. The cursor holds the last fingerprint acted on.

    Known limit: the cursor advances when the run is REQUESTED, not when it
    succeeds, so a run that fails after being requested is not re-fired by this
    sensor on its own (re-run it from the Dagster UI, or re-drop the file). A
    stricter success-gated cursor needs a run-status sensor, which is out of
    scope here; the weekly schedule also re-attempts.
    """
    missing = [n for n in RAW_GPKGS if not (RAW / n).is_file()]
    if missing:
        return SensorResult(skip_reason=f"waiting for raw inputs: {', '.join(missing)}")

    # Guard against a half-written file: a geopackage copied or downloaded
    # straight to its final path changes between stat calls while it is still
    # being written. Require the fingerprint to be stable across a short pause
    # before acting, so the run is requested only once the files settle.
    fp = fingerprint()
    time.sleep(2)
    if fp != fingerprint():
        return SensorResult(skip_reason="data/raw/ still changing; waiting for it to settle")

    if fp == (context.cursor or ""):
        return SensorResult(skip_reason="no new GRID3 vintage in data/raw/")

    from dagster import RunRequest

    # The run key carries the content fingerprint AND a short attempt suffix
    # derived from the current time, so (a) a failed refresh can be re-requested
    # for the same fingerprint instead of being deduplicated away, and (b) a
    # restored earlier vintage still triggers a run rather than being suppressed
    # by a reused key. The cursor stores only the fingerprint, so an unchanged
    # directory is still a no-op.
    run_key = f"{fp}-{int(time.time())}"
    return SensorResult(run_requests=[RunRequest(run_key=run_key)], cursor=fp)


defs = Definitions(
    assets=[raw_sources, dbt_models],
    jobs=[refresh_job],
    schedules=[weekly_refresh],
    sensors=[new_vintage_sensor],
)
