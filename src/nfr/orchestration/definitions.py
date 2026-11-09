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

import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from dagster import (
    AssetCheckResult,
    AssetSelection,
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

from nfr.orchestration import dashboard_cache, inputs, metrics, publish, snapshot, upstream

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "data" / "raw"
# The LGA frame and the derived enrichment tables the loader reads beside the
# geopackages. They are part of every run's snapshot and fingerprint.
FRAME = ROOT / "data" / inputs.FRAME_SUBDIR
DERIVED = ROOT / "data" / inputs.DERIVED_SUBDIR
# The live, reader-facing database. A refresh never builds here directly: it
# builds into a run-scoped side file under BUILD_DIR/staging and promotes that
# file onto this path only after the whole build and the quality gate pass.
DB = ROOT / "build" / "nfr.duckdb"
BUILD_DIR = ROOT / "build"
# Superset's chart-result cache (a redis:// URL naming its database). Set in the
# compose deployment; unset in the dev loop, where no Superset is running.
DASHBOARD_CACHE_URL_ENV = "NFR_SUPERSET_CACHE_REDIS_URL"
STAGING_DIR = BUILD_DIR / "staging"
# Per-run immutable copies of the inputs. A run snapshots data/raw/, data/frame/
# and data/derived/ here and loads from the snapshot, so a later change cannot alter
# what the run builds.
SNAPSHOTS_DIR = BUILD_DIR / "snapshots"
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
# Run tag carrying the population year the sensor fingerprinted, so a run
# launched just before 1 January still builds the year it was launched for.
POPULATION_YEAR_TAG = "nfr/population_year"
# The file in a run's snapshot that records the population year the run builds,
# so the publish step fingerprints and dbt builds the same year.
SNAPSHOT_YEAR_FILE = "population_year.txt"


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


def _snapshot_dir(run_id: str) -> Path:
    """Run-scoped directory holding this run's immutable copy of the raw inputs.

    Derived from the run id so two runs never share a snapshot; the loader reads
    from here, not the live input folders, so a change to them after the run
    starts cannot alter what it builds.
    """
    safe = "".join(c for c in run_id if c.isalnum() or c in "-_") or "manual"
    return SNAPSHOTS_DIR / safe

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
    """Snapshot the raw inputs, then load the snapshot into the run's DuckDB.

    The live inputs (data/raw/, data/frame/, data/derived/) are copied into a
    run-scoped immutable snapshot and the loader reads from the snapshot, so a
    change to them after the run starts cannot alter what this run builds. If
    the run was launched by the sensor for a specific fingerprint (the
    VINTAGE_FP_TAG), the snapshot's fingerprint is verified against it and the
    run fails fast if they differ, so a vintage that changed between the
    sensor's check and this load is never published under the old fingerprint.
    The population year is fixed here too (the sensor's tag, else today's) and
    recorded in the snapshot, so dbt builds exactly the year fingerprinted.
    """
    if missing := [n for n in RAW_GEOPACKAGES if not (RAW / n).exists()]:
        raise RuntimeError(
            f"missing raw inputs {missing}; fetch them with scripts/fetch_sources.py "
            "(HDX serves them behind a WAF, so the download is manual)"
        )
    if not (DERIVED / inputs.POPULATION_TABLE).is_file():
        raise RuntimeError(f"missing {DERIVED / inputs.POPULATION_TABLE}; the build needs the population table")

    # Immutable per-run snapshot of the inputs; the loader reads from here.
    # Everything after the snapshot is created runs under try/except, so ANY
    # failure (a tag mismatch, the loader, the count read) clears the snapshot
    # rather than leaving it under build/snapshots/ for a run that will not reach
    # dbt_models' cleanup. dbt_models re-creates nothing: on a successful
    # raw_sources the snapshot is left in place for it to read and then clear.
    snapshot_dir = _snapshot_dir(context.run_id)
    snapshot.create_snapshot(RAW, snapshot_dir, RAW_GEOPACKAGES)
    try:
        snapshot.create_snapshot(FRAME, snapshot_dir / inputs.FRAME_SUBDIR, inputs.input_files(FRAME))
        snapshot.create_snapshot(DERIVED, snapshot_dir / inputs.DERIVED_SUBDIR, inputs.input_files(DERIVED))
        population_year = _launch_population_year(context, snapshot_dir)
        (snapshot_dir / SNAPSHOT_YEAR_FILE).write_text(str(population_year))
        snapshot_fp = snapshot_fingerprint(snapshot_dir, population_year)

        # If the sensor launched this run for a specific vintage, confirm the
        # snapshot is that vintage. A mismatch means the inputs changed between
        # the sensor fingerprinting it and this snapshot, so the launched vintage
        # is no longer on disk; fail rather than publish the wrong bytes under
        # the requested fingerprint. The next sensor tick builds the new vintage.
        requested_fp = context.run.tags.get(VINTAGE_FP_TAG) if context.run else None
        if requested_fp and requested_fp != snapshot_fp:
            raise RuntimeError(
                f"inputs changed since launch: run requested vintage {requested_fp} "
                f"but the inputs now fingerprint as {snapshot_fp}. Not publishing; the next "
                "sensor tick will build the current vintage."
            )

        # Build into a run-scoped side file, never the live DB. A stale file from
        # a crashed earlier run with this id is removed first so the load starts
        # clean.
        staging_db = _staging_db_path(context.run_id)
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        staging_db.unlink(missing_ok=True)

        # Load from the SNAPSHOT (--raw), not the live directory. sys.executable,
        # not a bare "python", so the subprocess runs this interpreter/venv.
        subprocess.run(
            [sys.executable, "-m", "nfr.load_sources", "--db", str(staging_db),
             "--raw", str(snapshot_dir),
             "--frame", str(snapshot_dir / inputs.FRAME_SUBDIR),
             "--derived", str(snapshot_dir / inputs.DERIVED_SUBDIR)],
            cwd=str(ROOT), check=True,
        )
        # raw_counts, not load_counts: the staging tables do not exist until
        # dbt_models runs, so reading them here would fail a fresh refresh.
        counts = metrics.raw_counts_from_path(str(staging_db))
    except BaseException:
        snapshot.clear_snapshot(snapshot_dir)
        raise

    context.add_output_metadata(
        {
            "staging_db": str(staging_db),
            "snapshot_dir": str(snapshot_dir),
            "snapshot_fingerprint": snapshot_fp,
            "population_year": population_year,
            "raw_v2_rows": counts["raw_v2_rows"],
            "raw_v3_rows": counts["raw_v3_rows"],
        }
    )


def _promote(staging_db: Path, built_fingerprint: str, snapshot_dir: Path) -> None:
    """Publish a fully built and tested side database onto the live path.

    Records the published vintage in the side DB BEFORE the rename, so the
    fingerprint and the data it describes are promoted together in the one atomic
    step and can never disagree. os.replace is atomic within a filesystem (build
    and live share the build/ tree), so a reader of the live DB sees either the
    whole previous register or the whole new one, never a mixture.

    If either step raises, the side DB and the run's snapshot are removed, the
    live DB is left exactly as it was, and the error propagates.
    """
    try:
        publish.write_published_fingerprint(str(staging_db), built_fingerprint)
        os.replace(staging_db, DB)
    except BaseException:
        staging_db.unlink(missing_ok=True)
        snapshot.clear_snapshot(snapshot_dir)
        raise


def _clear_dashboard_cache(log) -> str:
    """Empty Superset's chart-result cache after a publish; return what happened.

    Runs after the new register is live, so a failure here must not fail the
    refresh: the data is already published, and the cache's own timeout bounds
    how long the dashboard can show the previous register. It is logged as a
    warning and recorded in the asset metadata instead.
    """
    url = os.environ.get(DASHBOARD_CACHE_URL_ENV)
    if not url:
        return "not configured"
    try:
        dashboard_cache.clear_dashboard_cache(url)
    except (OSError, ValueError, dashboard_cache.DashboardCacheError) as exc:
        log.warning(
            f"Published the new register but could not clear Superset's chart cache ({exc}). "
            "The dashboard may show the previous figures until the cache expires."
        )
        return f"not cleared: {exc}"
    return "cleared"


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

    # Fingerprint the SNAPSHOT this build is made from, not the live inputs:
    # raw_sources loaded the snapshot, which is immutable for the run, so this is
    # exactly the vintage being published even if the inputs have since changed. It
    # is recorded in the side database before promotion so the live DB always
    # knows which raw vintage it holds (the sensor reads this, not run history).
    snapshot_dir = _snapshot_dir(context.run_id)
    if not snapshot_dir.is_dir():
        raise RuntimeError(
            f"snapshot {snapshot_dir} is missing; raw_sources must run first"
        )
    population_year = int((snapshot_dir / SNAPSHOT_YEAR_FILE).read_text())
    built_fingerprint = snapshot_fingerprint(snapshot_dir, population_year)

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
            [sys.executable, "-m", "dbt.cli.main", "build", "--profiles-dir", ".",
             "--vars", f"{{population_year: {population_year}}}"],
            cwd=str(DBT_DIR), check=True, env=env,
        )
        # Enforce the hard carry-over gate against the staged build. Raises if
        # the rate does not clear the floor, so the side file is never promoted.
        _enforce_carryover_gate(str(staging_db))
        m = metrics.collect_from_path(str(staging_db))
    except BaseException:
        # Any failure (dbt build, a test, the gate, a cancellation) discards the
        # staged register and the run's snapshot, and leaves the live DB untouched.
        staging_db.unlink(missing_ok=True)
        snapshot.clear_snapshot(snapshot_dir)
        raise

    _promote(staging_db, built_fingerprint, snapshot_dir)
    dashboard_cache_status = _clear_dashboard_cache(context.log)
    context.add_output_metadata(
        {
            "dashboard_cache": dashboard_cache_status,
            "db": str(DB),
            "promoted_from": str(staging_db),
            "published_fingerprint": built_fingerprint,
            "authority_rows": m.authority_rows,
            "lineage_rows": m.lineage_rows,
            "coverage_lga_resolve_rate": _rate_meta(m.coverage_lga_resolve_rate),
            "drift_lga_resolve_rate": _rate_meta(m.drift_lga_resolve_rate),
            "carryover_rate": _rate_meta(m.carryover_rate),
        }
    )

    # The snapshot has served its purpose (loaded and published); remove it so
    # snapshots do not accumulate under build/snapshots/ run after run.
    snapshot.clear_snapshot(snapshot_dir)


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
    # The two refresh assets (and their checks) by name, not "*": the upstream
    # release watch below is a separate job and must never run inside, or hold
    # up, a refresh.
    selection=AssetSelection.assets("raw_sources", "dbt_models"),
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


def live_fingerprint(population_year: int) -> str:
    """Fingerprint the live inputs (data/raw/, data/frame/, data/derived/, the
    dbt project) for the given population year; see nfr.orchestration.inputs."""
    return inputs.fingerprint(RAW, RAW_GEOPACKAGES, FRAME, DERIVED,
                              inputs.dbt_project_digest(DBT_DIR), population_year)


def snapshot_fingerprint(snapshot_dir: Path, population_year: int) -> str:
    """Fingerprint a run's snapshot the same way as the live inputs, so the two
    agree exactly when the snapshot holds the inputs the sensor saw."""
    return inputs.fingerprint(snapshot_dir, RAW_GEOPACKAGES, snapshot_dir / inputs.FRAME_SUBDIR,
                              snapshot_dir / inputs.DERIVED_SUBDIR,
                              inputs.dbt_project_digest(DBT_DIR), population_year)


def _launch_population_year(context, snapshot_dir: Path) -> int:
    """The year the run was launched for (the sensor's tag), else today's.

    A schedule or manual run carries no tag, so it builds the year a build made
    today describes, chosen from the snapshot's own population table.
    """
    tagged = context.run.tags.get(POPULATION_YEAR_TAG) if context.run else None
    if tagged:
        return int(tagged)
    table = snapshot_dir / inputs.DERIVED_SUBDIR / inputs.POPULATION_TABLE
    return inputs.select_population_year(table, date.today())


def _running_or_queued_refresh(instance) -> bool:
    """Whether a refresh is already queued or in progress.

    Used to avoid stacking a second request behind a run that is already going
    to build the current inputs: the time-suffixed run key makes each tick a
    distinct request, and the concurrency limit serialises rather than
    deduplicates them, so without this check a vintage could be rebuilt once per
    tick until the first run finishes.
    """
    records = instance.get_run_records(
        filters=RunsFilter(
            job_name=refresh_job.name,
            statuses=[
                DagsterRunStatus.QUEUED,
                DagsterRunStatus.NOT_STARTED,
                DagsterRunStatus.STARTING,
                DagsterRunStatus.STARTED,
            ],
        ),
        limit=1,
    )
    return len(records) > 0


@sensor(
    job=refresh_job,
    minimum_interval_seconds=1800,
    default_status=DefaultSensorStatus.RUNNING,
)
def new_vintage_sensor(context: SensorEvaluationContext) -> SensorResult:
    """Fire the refresh when the raw inputs differ from the published vintage.

    Guards, in order:
    - Completeness: every raw input must be present, so the sensor never fires
      on a half-applied set (one new file beside one still-missing one).
    - Settle across two ticks: the first tick that sees a new fingerprint records
      it in the cursor and waits; the sensor fires only once the SAME fingerprint
      is still present on a later tick. This replaces an inline time.sleep, which
      blocked Dagster's shared sensor daemon thread. A file still being written,
      or a half-applied set mid-swap, changes the fingerprint between ticks and so
      never fires.
    - Published-vintage gate: the sensor fires only when the settled fingerprint
      differs from the one the live database was last built from (recorded by the
      promote step, read via publish.read_published_fingerprint). The live DB is
      the authority on what is published, which means: a run that fails or is
      still queued records nothing, so the vintage stays eligible; any launch
      path that promotes (schedule, manual, sensor) updates the sentinel, so the
      sensor sees it; and a restored older vintage differs from what is live, so
      it is rebuilt rather than skipped while stale data stays live.
    - No double-dispatch: if a refresh is already queued or running, skip, so the
      per-tick run key does not stack duplicate requests behind it.
    """
    if missing := [n for n in RAW_GEOPACKAGES if not (RAW / n).is_file()]:
        return SensorResult(skip_reason=f"waiting for raw inputs: {', '.join(missing)}")
    if not (DERIVED / inputs.POPULATION_TABLE).is_file():
        return SensorResult(skip_reason=f"waiting for {inputs.POPULATION_TABLE}")

    population_year = inputs.select_population_year(DERIVED / inputs.POPULATION_TABLE, date.today())
    fp = live_fingerprint(population_year)

    # Two-tick settle: the cursor holds the fingerprint last seen. Only act once
    # the same fingerprint has persisted across two evaluations, so a file still
    # settling (its fingerprint changing tick to tick) is never acted on.
    if fp != (context.cursor or ""):
        return SensorResult(
            cursor=fp,
            skip_reason="the inputs changed since the last check; confirming they have settled",
        )

    published = publish.read_published_fingerprint(str(DB))
    if fp == published:
        return SensorResult(cursor=fp, skip_reason=f"vintage {fp} is already published")

    if _running_or_queued_refresh(context.instance):
        return SensorResult(cursor=fp, skip_reason="a refresh is already queued or running")

    # The run key carries the fingerprint AND a time suffix so a re-fire after a
    # failed run is a new, non-deduplicated request. The run is tagged with the
    # fingerprint for traceability in the UI.
    run_key = f"{fp}-{int(time.time())}"
    return SensorResult(
        run_requests=[RunRequest(run_key=run_key, tags={VINTAGE_FP_TAG: fp,
                                                        POPULATION_YEAR_TAG: str(population_year)})],
        cursor=fp,
    )


@asset(group_name="upstream_watch")
def upstream_releases(context) -> None:
    """Record whether each upstream publisher has released something newer than what is pinned.

    Reads only public catalogue pages (WorldPop's release index, HDX's catalogue
    API, MAP's WCS capabilities); it downloads no data and never touches the
    register, so a new release reaches the register only through a reviewed
    change to data/sources.yml and the committed tables. See nfr.orchestration.upstream.
    """
    findings = upstream.check_upstream()
    for finding in findings:
        log = context.log.warning if finding.status is not upstream.Status.CURRENT else context.log.info
        log(f"{finding.source}: {finding.status.value}: {finding.detail}")
    context.add_output_metadata({
        f.source: MetadataValue.text(f"{f.status.value}: {f.detail}") for f in findings
    } | {
        "needs_review": sum(f.status is upstream.Status.NEW_RELEASE for f in findings),
        "unreachable": sum(f.status is upstream.Status.UNREACHABLE for f in findings),
        "unreadable": sum(f.status is upstream.Status.UNREADABLE for f in findings),
    })


@asset_check(asset=upstream_releases, blocking=False)
def no_unreviewed_upstream_release(context) -> AssetCheckResult:
    """Warn (never fail a job) while a newer upstream release is waiting for review, a
    publisher is unreachable or unreadable, or no check has run yet.

    Reads the findings the latest upstream_releases materialisation recorded, so
    the check does not hit the network a second time.
    """
    event = context.instance.get_latest_materialization_event(upstream_releases.key)
    if event is None or event.asset_materialization is None:
        # Nothing has been checked yet, which is not the same as nothing to review.
        return AssetCheckResult(
            passed=False,
            severity=AssetCheckSeverity.WARN,
            description="upstream_releases has never been materialised; run check_upstream_releases first",
        )
    metadata = event.asset_materialization.metadata
    counts = {
        name: int(getattr(metadata.get(name), "value", 0) or 0)
        for name in ("needs_review", "unreachable", "unreadable")
    }
    return AssetCheckResult(
        passed=not any(counts.values()),
        severity=AssetCheckSeverity.WARN,
        description=(
            f"{counts['needs_review']} upstream release(s) to review, {counts['unreachable']} publisher(s) "
            f"unreachable, {counts['unreadable']} answering in an unexpected format; "
            "see the upstream_releases materialisation for details"
        ),
        metadata=counts,
    )


upstream_check_job = define_asset_job(
    "check_upstream_releases",
    selection=AssetSelection.assets("upstream_releases"),
)

# Monthly: upstream publishers release a few times a year at most, and the check
# only reads catalogue pages, so a monthly look is timely without being noisy.
monthly_upstream_check = ScheduleDefinition(
    job=upstream_check_job,
    cron_schedule="0 7 1 * *",  # 1st of the month, 07:00
    name="monthly_upstream_check",
    default_status=DefaultScheduleStatus.RUNNING,
)


@run_failure_sensor(monitored_jobs=[refresh_job], default_status=DefaultSensorStatus.RUNNING)
def refresh_failure_sensor(context) -> None:
    """Log a failed refresh so the failure is visible in the sensor timeline.

    The re-fire itself is handled by new_vintage_sensor's published-vintage gate
    (a failed run never updates the sentinel, so the live fingerprint still
    differs from the raw inputs and the next tick re-fires); this sensor only
    records the failure for operators. It does not touch the vintage
    sensor's cursor, which would be a fragile cross-sensor write.
    """
    context.log.warning(
        f"refresh_register run {context.dagster_run.run_id} failed; the vintage will be "
        "re-fired by new_vintage_sensor on the next tick because its fingerprint has no "
        "successful run."
    )


defs = Definitions(
    assets=[raw_sources, dbt_models, upstream_releases],
    asset_checks=[
        lga_resolve_rate_check, carryover_rate_check, carryover_gate_check, no_unreviewed_upstream_release,
    ],
    jobs=[refresh_job, upstream_check_job],
    schedules=[weekly_refresh, monthly_upstream_check],
    sensors=[new_vintage_sensor, refresh_failure_sensor],
)
