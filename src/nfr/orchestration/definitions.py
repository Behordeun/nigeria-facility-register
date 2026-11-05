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

import duckdb

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    DefaultScheduleStatus,
    DefaultSensorStatus,
    Definitions,
    MetadataValue,
    ScheduleDefinition,
    SensorEvaluationContext,
    SensorResult,
    asset,
    asset_check,
    define_asset_job,
    sensor,
)

from nfr.orchestration import metrics

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "data" / "raw"
DB = ROOT / "build" / "nfr.duckdb"
DBT_DIR = ROOT / "dbt"
RAW_GPKGS = ["nga_health_facilities_v2_0.gpkg", "grid3_nga_health_facilities_v3_0.gpkg"]

# Advisory floors for the asset checks. A refresh below one of these is not
# necessarily broken, but it is worth a human glance, so the checks warn rather
# than fail the run. They are deliberately loose: the LGA resolve rate sits well
# above 0.90 on the real vintages and the carry-over rate near 0.90, so these
# catch a collapse (a crosswalk regression, a bad load), not normal variation.
MIN_LGA_RESOLVE_RATE = 0.80
MIN_CARRYOVER_RATE = 0.60

# Hard floor for the data-quality gate, far below the advisory warn floor above.
# A carry-over this low is not variation, it is a broken match (a coordinate or
# projection change, a key swap) producing a lineage that is almost all drops
# and appearances. The gate check that reads it is BLOCKING: it fails the run so
# the bad lineage is never served, where the warn check only flags a dip.
HARD_MIN_CARRYOVER_RATE = 0.30


def _rate_meta(value: float | None) -> MetadataValue:
    """Render a rate as a percentage for the Dagster UI, or n/a when undefined."""
    return MetadataValue.text("n/a" if value is None else f"{value * 100:.1f}%")


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
    # raw_counts, not load_counts: the staging tables do not exist until
    # dbt_models runs, so reading them here would fail a fresh refresh.
    counts = metrics.raw_counts_from_path(str(DB))
    context.add_output_metadata(
        {
            "db": str(DB),
            "raw_v2_rows": counts["raw_v2_rows"],
            "raw_v3_rows": counts["raw_v3_rows"],
        }
    )


def _enforce_carryover_gate(db_path: str) -> float | None:
    """Fail the refresh when the carry-over rate does not clear the hard floor.

    Runs inside the dbt_models asset, after the build, so a failure raises and
    the asset (and the whole refresh_register job) fails. A blocking asset check
    would only block assets downstream of dbt_models, and dbt_models is terminal,
    so the run would report success while serving a broken lineage. Enforcing it
    here is what actually stops the run.

    A None rate is a failure, not a pass: by the time the gate runs on a real
    GRID3 load there is always a v2.0 population in the v3-covered states, so
    None means the lineage produced no comparable facilities at all (unusable
    input, a load that read rows but no usable ids or coordinates), which is
    exactly the malformed case the gate exists to stop.

    On failure the mart and lineage tables are dropped from the canonical DB
    before raising, so a reader pointed at the shared file never sees the
    rejected lineage. This does not make the build fully transactional (the
    tables exist during the build itself, before the gate runs), but it ensures
    a gate rejection is never left readable; full staging-schema atomicity is
    tracked separately.
    """
    rate = metrics.carryover_rate_from_path(db_path)
    passed = rate is not None and rate >= HARD_MIN_CARRYOVER_RATE
    if passed:
        return rate

    con = duckdb.connect(db_path)
    try:
        for table in ("lga_version_drift", "lga_coverage_burden", "stg_version_lineage"):
            con.execute(f"drop table if exists {table}")
    finally:
        con.close()
    shown = "undefined (no comparable v2.0 population)" if rate is None else f"{rate * 100:.1f}%"
    raise RuntimeError(
        f"data-quality gate failed: carry-over rate {shown} is below the hard floor "
        f"{HARD_MIN_CARRYOVER_RATE * 100:.1f}%. The rejected marts were dropped and the "
        "refresh is failed so the broken lineage is not served."
    )


@asset(deps=[raw_sources])
def dbt_models(context) -> None:
    """Run the dbt staging and marts build, enforce the quality gate, then emit
    the run's metrics.

    The hard carry-over gate runs here, not as a blocking asset check: raising
    in the asset fails the refresh job, where a check downstream of a terminal
    asset would not. The dbt tests (referential integrity, row-count bounds)
    already fail the build inside `dbt build` above.
    """
    # Run dbt as a module of this interpreter, so it resolves to the dbt beside
    # the virtualenv's Python rather than whatever is on the system PATH.
    subprocess.run(
        [sys.executable, "-m", "dbt", "build", "--profiles-dir", "."],
        cwd=str(DBT_DIR), check=True,
    )
    # Enforce the hard carry-over gate before reporting success. Raises (failing
    # the job) and drops the rejected marts if the rate does not clear the floor.
    _enforce_carryover_gate(str(DB))
    m = metrics.collect_from_path(str(DB))
    context.add_output_metadata(
        {
            "authority_rows": m.authority_rows,
            "lineage_rows": m.lineage_rows,
            "coverage_lga_resolve_rate": _rate_meta(m.coverage_lga_resolve_rate),
            "drift_lga_resolve_rate": _rate_meta(m.drift_lga_resolve_rate),
            "carryover_rate": _rate_meta(m.carryover_rate),
        }
    )


@asset_check(asset=dbt_models, blocking=False)
def lga_resolve_rate_check(context) -> AssetCheckResult:
    """Warn when too little of the frame resolved to a facility.

    A collapse in the coverage resolve rate points at a crosswalk regression or
    a bad load, not normal vintage variation. Undefined (no LGAs) passes with a
    note rather than asserting a floor against a missing denominator.
    """
    rate = metrics.coverage_lga_resolve_rate_from_path(str(DB))
    if rate is None:
        return AssetCheckResult(
            passed=True, metadata={"coverage_lga_resolve_rate": _rate_meta(None)}
        )
    return AssetCheckResult(
        passed=rate >= MIN_LGA_RESOLVE_RATE,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "coverage_lga_resolve_rate": _rate_meta(rate),
            "floor": _rate_meta(MIN_LGA_RESOLVE_RATE),
        },
    )


@asset_check(asset=dbt_models, blocking=False)
def carryover_rate_check(context) -> AssetCheckResult:
    """Warn when too few v2.0 facilities carried over into v3.0.

    A sharp drop in carry-over suggests the spatial match broke (a coordinate or
    projection change), since a real vintage carries close to 90% forward.
    Undefined (no v2.0 facilities in a covered state) passes with a note.
    """
    rate = metrics.carryover_rate_from_path(str(DB))
    if rate is None:
        return AssetCheckResult(passed=True, metadata={"carryover_rate": _rate_meta(None)})
    return AssetCheckResult(
        passed=rate >= MIN_CARRYOVER_RATE,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "carryover_rate": _rate_meta(rate),
            "floor": _rate_meta(MIN_CARRYOVER_RATE),
        },
    )


# The hard carry-over gate is enforced inside the dbt_models asset
# (_enforce_carryover_gate), not as a blocking asset check: a check downstream
# of the terminal dbt_models asset would not fail the job, so the run would
# report success while serving a broken lineage. The warn-severity checks above
# stay as asset checks because their job is visibility, not stopping the run.


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
    asset_checks=[lga_resolve_rate_check, carryover_rate_check],
    jobs=[refresh_job],
    schedules=[weekly_refresh],
    sensors=[new_vintage_sensor],
)
