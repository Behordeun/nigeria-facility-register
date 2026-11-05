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
import os
import subprocess
import sys
import time
from pathlib import Path

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    DagsterRunStatus,
    DefaultScheduleStatus,
    DefaultSensorStatus,
    Definitions,
    MetadataValue,
    RunRequest,
    RunsFilter,
    ScheduleDefinition,
    SensorEvaluationContext,
    SensorResult,
    asset,
    asset_check,
    define_asset_job,
    run_failure_sensor,
    sensor,
)

from nfr.orchestration import metrics

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "data" / "raw"
# The live, reader-facing database. A refresh never builds here directly: it
# builds into a run-scoped side file under BUILD_DIR/staging and promotes that
# file onto this path only after the whole build and the quality gate pass.
DB = ROOT / "build" / "nfr.duckdb"
BUILD_DIR = ROOT / "build"
STAGING_DIR = BUILD_DIR / "staging"
# The env var the dbt profile reads for its DuckDB path. The orchestrated run
# sets it to the run-scoped side file; unset (a plain make build) it defaults to
# the live DB inside profiles.yml.
DBT_DB_ENV = "NFR_DUCKDB_PATH"
DBT_DIR = ROOT / "dbt"
RAW_GEOPACKAGES = ["nga_health_facilities_v2_0.gpkg", "grid3_nga_health_facilities_v3_0.gpkg"]

# Run tag carrying the raw-input fingerprint a refresh was launched for. The
# vintage sensor reads it back to tell whether a given vintage already SUCCEEDED,
# which is what success-gates the sensor (see new_vintage_sensor).
VINTAGE_FP_TAG = "nfr/vintage_fingerprint"


def _staging_db_path(run_id: str) -> Path:
    """Run-scoped side database both refresh assets build into.

    Derived from the Dagster run id so a run never collides with the live DB or
    with another run's side file. The run coordinator already serialises runs on
    the refresh concurrency key, so at most one is building at a time; the
    run-scoped name is belt-and-braces and keeps a crashed run's leftover file
    identifiable.
    """
    # No interior dot in the stem: dbt-duckdb reads a path like nfr.<id>.duckdb
    # as a catalog.schema reference and fails to bind. Separate with an
    # underscore so the only dot is before the extension.
    safe = "".join(c for c in run_id if c.isalnum() or c in "-_") or "manual"
    return STAGING_DIR / f"nfr_{safe}.duckdb"

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
    if missing := [n for n in RAW_GEOPACKAGES if not (RAW / n).exists()]:
        raise RuntimeError(
            f"missing raw inputs {missing}; fetch them with scripts/fetch_sources.py "
            "(HDX serves them behind a WAF, so the download is manual)"
        )
    # Build into a run-scoped side file, never the live DB. A fresh side file is
    # started here and dbt_models continues into the same one; only after the
    # gate passes is it promoted onto the live path. A stale file from a crashed
    # earlier run with this id is removed first so the load starts clean.
    staging_db = _staging_db_path(context.run_id)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staging_db.unlink(missing_ok=True)

    # sys.executable, not a bare "python", so the subprocess runs the same
    # interpreter (and virtualenv) as the Dagster process, with the project
    # dependencies available.
    subprocess.run(
        [sys.executable, "-m", "nfr.load_sources", "--db", str(staging_db)],
        cwd=str(ROOT), check=True,
    )
    # raw_counts, not load_counts: the staging tables do not exist until
    # dbt_models runs, so reading them here would fail a fresh refresh.
    counts = metrics.raw_counts_from_path(str(staging_db))
    context.add_output_metadata(
        {
            "staging_db": str(staging_db),
            "raw_v2_rows": counts["raw_v2_rows"],
            "raw_v3_rows": counts["raw_v3_rows"],
        }
    )


def _enforce_carryover_gate(db_path: str) -> float:
    """Return the carry-over rate, or raise if it does not clear the hard floor.

    Runs inside the dbt_models asset against the run-scoped side database, before
    that database is promoted onto the live path. Raising here fails the asset
    and the whole refresh job, and the side file is never promoted, so a reader
    of the live DB never sees the rejected lineage. The gate no longer drops
    tables: the rejected register is a throwaway side file, discarded whole, not
    the live database.

    A None rate is a failure, not a pass: by the time the gate runs on a real
    GRID3 load there is always a v2.0 population in the v3-covered states, so
    None means the lineage produced no comparable facilities at all (unusable
    input, a load that read rows but no usable ids or coordinates), which is
    exactly the malformed case the gate exists to stop.
    """
    rate = metrics.carryover_rate_from_path(db_path)
    if rate is not None and rate >= HARD_MIN_CARRYOVER_RATE:
        return rate
    shown = "undefined (no comparable v2.0 population)" if rate is None else f"{rate * 100:.1f}%"
    raise RuntimeError(
        f"data-quality gate failed: carry-over rate {shown} is below the hard floor "
        f"{HARD_MIN_CARRYOVER_RATE * 100:.1f}%. The staged register is discarded and the "
        "refresh is failed, so the live register is left untouched."
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
    staging_db = _staging_db_path(context.run_id)
    if not staging_db.is_file():
        raise RuntimeError(
            f"staging database {staging_db} is missing; raw_sources must run first"
        )

    try:
        # Build dbt against the SIDE database, not the live one: the profile
        # reads NFR_DUCKDB_PATH. dbt build runs the models and the data-quality
        # tests, so a referential or row-count failure aborts here before any
        # promote. sys.executable keeps the subprocess on this venv's dbt.
        env = {**os.environ, DBT_DB_ENV: str(staging_db)}
        # `python -m dbt.cli.main`, not `-m dbt`: dbt ships no dbt.__main__, so
        # `-m dbt` fails with "No module named dbt.__main__". Running the cli
        # module keeps the subprocess on this interpreter's venv dbt.
        subprocess.run(
            [sys.executable, "-m", "dbt.cli.main", "build", "--profiles-dir", "."],
            cwd=str(DBT_DIR), check=True, env=env,
        )
        # Enforce the hard carry-over gate against the staged build. Raises if
        # the rate does not clear the floor, so the side file is never promoted.
        _enforce_carryover_gate(str(staging_db))
        m = metrics.collect_from_path(str(staging_db))
    except BaseException:
        # Any failure (dbt build, a test, the gate, a cancellation) discards the
        # staged register and leaves the live DB untouched.
        staging_db.unlink(missing_ok=True)
        raise

    # Everything passed: promote the staged database onto the live path in one
    # atomic rename. os.replace is atomic within a filesystem, so a reader of the
    # live DB sees either the whole previous register or the whole new one, never
    # a half-built or rejected mixture. Build and live sit under the same build/
    # tree, so the rename stays on one filesystem.
    os.replace(staging_db, DB)
    context.add_output_metadata(
        {
            "db": str(DB),
            "promoted_from": str(staging_db),
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


@asset_check(asset=dbt_models, blocking=False)
def carryover_gate_check(context) -> AssetCheckResult:
    """Report the hard carry-over gate's verdict as an asset-check result.

    The gate is ENFORCED inside the dbt_models asset (_enforce_carryover_gate),
    which raises and fails the refresh job on a collapse, because a blocking
    check downstream of the terminal dbt_models asset would not fail the job.
    This check is the reporting half: on a completed build it records the gate
    verdict against the hard floor, so the Dagster UI and any consumer that
    reads asset-check results sees whether the gate passed, not only the warn
    check. On a collapse the asset has already raised, so the run fails and no
    result is recorded here, which is the correct signal. An undefined rate is
    reported as failed, matching the enforcement.
    """
    rate = metrics.carryover_rate_from_path(str(DB))
    passed = rate is not None and rate >= HARD_MIN_CARRYOVER_RATE
    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        metadata={
            "carryover_rate": _rate_meta(rate),
            "hard_floor": _rate_meta(HARD_MIN_CARRYOVER_RATE),
        },
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
    for n in names or RAW_GEOPACKAGES:
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


def _vintage_has_succeeded(instance, fp: str) -> bool:
    """Whether a refresh for this exact fingerprint has already SUCCEEDED.

    Queries completed runs tagged with the fingerprint. This is what
    success-gates the sensor: it fires for a fingerprint only while no
    successful run exists for it, so a run that fails (or was requested but
    never finished) leaves the vintage eligible and the next tick re-fires it,
    while a vintage that genuinely succeeded is never rebuilt. A self-advanced
    cursor could not express this: it advances on request, not on success.
    """
    records = instance.get_run_records(
        filters=RunsFilter(
            statuses=[DagsterRunStatus.SUCCESS],
            tags={VINTAGE_FP_TAG: fp},
        ),
        limit=1,
    )
    return len(records) > 0


@sensor(
    job=refresh_job,
    minimum_interval_seconds=3600,
    default_status=DefaultSensorStatus.RUNNING,
)
def new_vintage_sensor(context: SensorEvaluationContext) -> SensorResult:
    """Fire the refresh when a new, un-built GRID3 vintage is present in data/raw/.

    Three guards, in order:
    - Completeness: every raw input must be present, so the sensor never fires
      on a half-applied set (one new file beside one still-missing one).
    - Settle: the combined content fingerprint must be stable across a short
      pause, so a file still being written is not read mid-copy. Placement via
      scripts/fetch_sources.py --place already makes each file land atomically
      and as a set; this is the belt-and-braces guard for a hand-dropped file.
    - Success-gating: the sensor fires only while no SUCCEEDED run exists for the
      current fingerprint. A run that fails leaves the vintage eligible, so the
      next tick re-fires it; a vintage that succeeded is never rebuilt. This
      replaces the old request-time cursor, which skipped a vintage whose run had
      failed. The cursor is kept only as a cheap "nothing changed since last
      tick" short-circuit, not as the authority.
    """
    if missing := [n for n in RAW_GEOPACKAGES if not (RAW / n).is_file()]:
        return SensorResult(skip_reason=f"waiting for raw inputs: {', '.join(missing)}")

    fp = fingerprint()
    time.sleep(2)
    if fp != fingerprint():
        return SensorResult(skip_reason="data/raw/ still changing; waiting for it to settle")

    # Success gate: skip only if this exact vintage already built successfully.
    # The cursor short-circuit below is just an optimization; the success query
    # is the authority, so a failed run's fingerprint is not suppressed.
    if _vintage_has_succeeded(context.instance, fp):
        context.update_cursor(fp)
        return SensorResult(skip_reason=f"vintage {fp} already built successfully")

    # The run key carries the fingerprint AND a time suffix so a re-fire after a
    # failed run is a new, non-deduplicated request. The run is tagged with the
    # fingerprint so a later tick can see whether it succeeded.
    run_key = f"{fp}-{int(time.time())}"
    return SensorResult(
        run_requests=[RunRequest(run_key=run_key, tags={VINTAGE_FP_TAG: fp})],
        cursor=fp,
    )


@run_failure_sensor(monitored_jobs=[refresh_job], default_status=DefaultSensorStatus.RUNNING)
def refresh_failure_sensor(context) -> None:
    """Log a failed refresh so the failure is visible in the sensor timeline.

    The re-fire itself is handled by new_vintage_sensor's success gate (a failed
    run leaves its fingerprint un-succeeded, so the next tick re-fires it); this
    sensor only records the failure for operators. It does not touch the vintage
    sensor's cursor, which would be a fragile cross-sensor write.
    """
    context.log.warning(
        f"refresh_register run {context.dagster_run.run_id} failed; the vintage will be "
        "re-fired by new_vintage_sensor on the next tick because its fingerprint has no "
        "successful run."
    )


defs = Definitions(
    assets=[raw_sources, dbt_models],
    asset_checks=[lga_resolve_rate_check, carryover_rate_check, carryover_gate_check],
    jobs=[refresh_job],
    schedules=[weekly_refresh],
    sensors=[new_vintage_sensor, refresh_failure_sensor],
)
