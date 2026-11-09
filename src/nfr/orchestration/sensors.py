"""
The new-vintage sensor that launches a refresh when the inputs change, and the
sensor that records a failed refresh.
"""
from __future__ import annotations

import time
from datetime import date

from dagster import (
    DagsterRunStatus,
    DefaultSensorStatus,
    RunRequest,
    RunsFilter,
    SensorEvaluationContext,
    SensorResult,
    run_failure_sensor,
    sensor,
)

from nfr.orchestration import config, inputs, publish, registers
from nfr.orchestration.jobs import refresh_job

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
    - Rollback hold: a vintage held after a restore is not requested again (the
      run itself would also refuse it); the hold lifts when the inputs change.
    - No double-dispatch: if a refresh is already queued or running, skip, so the
      per-tick run key does not stack duplicate requests behind it.
    """
    if missing := [n for n in config.RAW_GEOPACKAGES if not (config.RAW / n).is_file()]:
        return SensorResult(skip_reason=f"waiting for raw inputs: {', '.join(missing)}")
    if not (config.DERIVED / inputs.POPULATION_TABLE).is_file():
        return SensorResult(skip_reason=f"waiting for {inputs.POPULATION_TABLE}")

    # An unreadable population table (empty, or mid-write) is a reason to wait,
    # not a sensor error: skipping keeps the sensor healthy and says why.
    try:
        population_year = inputs.select_population_year(config.DERIVED / inputs.POPULATION_TABLE, date.today())
    except ValueError as exc:
        return SensorResult(skip_reason=f"waiting for a readable population table: {exc}")
    fp = config.live_fingerprint(population_year)

    # Two-tick settle: the cursor holds the fingerprint last seen. Only act once
    # the same fingerprint has persisted across two evaluations, so a file still
    # settling (its fingerprint changing tick to tick) is never acted on.
    if fp != (context.cursor or ""):
        return SensorResult(
            cursor=fp,
            skip_reason="the inputs changed since the last check; confirming they have settled",
        )

    published = publish.read_published_fingerprint(str(config.DB))
    if fp == published:
        return SensorResult(cursor=fp, skip_reason=f"vintage {fp} is already published")

    if fp == registers.read_hold(config.HOLD_FILE):
        return SensorResult(cursor=fp, skip_reason=f"vintage {fp} is held after a rollback; change the inputs or release the hold")

    if _running_or_queued_refresh(context.instance):
        return SensorResult(cursor=fp, skip_reason="a refresh is already queued or running")

    # The run key carries the fingerprint AND a time suffix so a re-fire after a
    # failed run is a new, non-deduplicated request. The run is tagged with the
    # fingerprint for traceability in the UI.
    run_key = f"{fp}-{int(time.time())}"
    return SensorResult(
        run_requests=[RunRequest(run_key=run_key, tags={config.VINTAGE_FP_TAG: fp,
                                                        config.POPULATION_YEAR_TAG: str(population_year)})],
        cursor=fp,
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
