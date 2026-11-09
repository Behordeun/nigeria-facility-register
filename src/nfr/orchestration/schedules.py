"""
When the refresh and the upstream-release check run on their own.
"""
from __future__ import annotations

from dagster import DefaultScheduleStatus, ScheduleDefinition

from nfr.orchestration.jobs import refresh_job, upstream_check_job

# Weekly, not daily: GRID3 releases are infrequent, so a weekly rebuild keeps the
# marts current without spinning for no reason. default_status RUNNING so the
# schedule is active on load without a manual toggle.
weekly_refresh = ScheduleDefinition(
    job=refresh_job,
    cron_schedule="0 6 * * 1",  # Monday 06:00
    name="weekly_refresh",
    default_status=DefaultScheduleStatus.RUNNING,
)

# Monthly: upstream publishers release a few times a year at most, and the check
# only reads catalogue pages, so a monthly look is timely without being noisy.
monthly_upstream_check = ScheduleDefinition(
    job=upstream_check_job,
    cron_schedule="0 7 1 * *",  # 1st of the month, 07:00
    name="monthly_upstream_check",
    default_status=DefaultScheduleStatus.RUNNING,
)
