"""
The refresh's quality gates and its publish step.

The carry-over gate decides whether a staged register may be published; the
promote step publishes it atomically with the fingerprint it was built from;
the dashboard cache is cleared once the new register is live. The advisory
floors here are read by the asset checks.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

from dagster import MetadataValue

from nfr.orchestration import alerts, changes, config, dashboard_cache, metrics, publish, registers, snapshot

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


def rate_meta(value: float | None) -> MetadataValue:
    """Render a rate as a percentage for the Dagster UI, or n/a when undefined."""
    return MetadataValue.text("n/a" if value is None else f"{value * 100:.1f}%")


def check_staged_build(staging_db: Path, built_fingerprint: str, snapshot_dir: Path) -> metrics.RunMetrics:
    """Gate a staged build, read its metrics and record what it changes; discard it on failure.

    The carry-over gate decides whether the build may be published at all. The
    change record (register_changes) is written into the build against the live
    register, which is only read. Any failure here (the gate, the metrics read,
    the change record, a cancellation) removes the staged build and the run's
    snapshot and leaves the live register untouched, so nothing reaches promote.
    """
    try:
        enforce_carryover_gate(str(staging_db))
        run_metrics = metrics.collect_from_path(str(staging_db))
        changes.write_register_changes(staging_db, config.DB, built_fingerprint,
                                       publish.read_published_fingerprint(str(config.DB)))
    except BaseException:
        staging_db.unlink(missing_ok=True)
        snapshot.clear_snapshot(snapshot_dir)
        raise
    return run_metrics


def promote(staging_db: Path, built_fingerprint: str, snapshot_dir: Path) -> None:
    """Publish a fully built and tested side database onto the live path.

    Records the published vintage in the side DB BEFORE the rename, so the
    fingerprint and the data it describes are promoted together in the one atomic
    step and can never disagree. os.replace is atomic within a filesystem (build
    and live share the build/ tree), so a reader of the live DB sees either the
    whole previous register or the whole new one, never a mixture.

    The outgoing register is hard-linked into the archive first (no copy; the
    live path stays in place until the rename replaces it), so it can be restored
    with scripts/restore_register.py; failing to archive it fails the publish,
    so every register that was ever live can be rolled back to while it is kept.

    If any of those steps raises, the side DB and the run's snapshot are removed,
    the live DB is left exactly as it was, and the error propagates. Pruning the
    archive and lifting a rollback hold happen after the register is live, so
    they are best-effort: a failure there never un-publishes a good register.
    """
    try:
        publish.write_published_fingerprint(str(staging_db), built_fingerprint)
        registers.archive_live(config.DB, config.ARCHIVE_DIR)
        os.replace(staging_db, config.DB)
    except BaseException:
        staging_db.unlink(missing_ok=True)
        snapshot.clear_snapshot(snapshot_dir)
        raise
    # A different register is now live, so a hold on a rolled-back vintage no
    # longer applies. The refresh refuses to build a held vintage at all; the
    # comparison keeps the hold should any other path ever publish that vintage.
    with contextlib.suppress(OSError):
        registers.prune(config.ARCHIVE_DIR, config.ARCHIVE_KEEP)
    with contextlib.suppress(OSError):
        if built_fingerprint != registers.read_hold(config.HOLD_FILE):
            registers.clear_hold(config.HOLD_FILE)
    # A publish ends any run of failures, so the next failure alerts again.
    with contextlib.suppress(OSError):
        alerts.reset(config.ALERT_STATE_FILE)


def clear_dashboard_cache(log) -> str:
    """Empty Superset's chart-result cache after a publish; return what happened.

    Runs after the new register is live, so a failure here must not fail the
    refresh: the data is already published, and the cache's own timeout bounds
    how long the dashboard can show the previous register. It is logged as a
    warning and recorded in the asset metadata instead.
    """
    url = os.environ.get(config.DASHBOARD_CACHE_URL_ENV)
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


def enforce_carryover_gate(db_path: str) -> float:
    """Return the carry-over rate, or raise if it does not clear the hard floor.

    Runs inside the published_register asset against the run-scoped side database, before
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
