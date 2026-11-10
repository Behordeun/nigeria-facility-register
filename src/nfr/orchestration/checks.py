"""
Asset checks: advisory floors and the gate verdict on the published register,
and whether an upstream release is waiting for review.
"""
from __future__ import annotations

from dagster import AssetCheckResult, AssetCheckSeverity, asset_check

from nfr.orchestration import config, gates, metrics
from nfr.orchestration.assets import published_register, upstream_releases

@asset_check(asset=published_register, blocking=False)
def lga_resolve_rate_check(context) -> AssetCheckResult:
    """Warn when too little of the frame resolved to a facility.

    A collapse in the coverage resolve rate points at a crosswalk regression or
    a bad load, not normal vintage variation. Undefined (no LGAs) passes with a
    note rather than asserting a floor against a missing denominator.
    """
    rate = metrics.coverage_lga_resolve_rate_from_path(str(config.DB))
    if rate is None:
        return AssetCheckResult(
            passed=True, metadata={"coverage_lga_resolve_rate": gates.rate_meta(None)}
        )
    return AssetCheckResult(
        passed=rate >= gates.MIN_LGA_RESOLVE_RATE,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "coverage_lga_resolve_rate": gates.rate_meta(rate),
            "floor": gates.rate_meta(gates.MIN_LGA_RESOLVE_RATE),
        },
    )


@asset_check(asset=published_register, blocking=False)
def carryover_rate_check(context) -> AssetCheckResult:
    """Warn when too few v2.0 facilities carried over into v3.0.

    A sharp drop in carry-over suggests the spatial match broke (a coordinate or
    projection change), since a real vintage carries close to 90% forward.
    Undefined (no v2.0 facilities in a covered state) passes with a note.
    """
    rate = metrics.carryover_rate_from_path(str(config.DB))
    if rate is None:
        return AssetCheckResult(passed=True, metadata={"carryover_rate": gates.rate_meta(None)})
    return AssetCheckResult(
        passed=rate >= gates.MIN_CARRYOVER_RATE,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "carryover_rate": gates.rate_meta(rate),
            "floor": gates.rate_meta(gates.MIN_CARRYOVER_RATE),
        },
    )


@asset_check(asset=published_register, blocking=False)
def carryover_gate_check(context) -> AssetCheckResult:
    """Report the hard carry-over gate's verdict as an asset-check result.

    The gate is ENFORCED inside the published_register asset
    (gates.enforce_carryover_gate), which raises and fails the refresh job on a
    collapse, because a blocking check on the terminal published_register asset
    would not fail the job.
    This check is the reporting half: on a completed build it records the gate
    verdict against the hard floor, so the Dagster UI and any consumer that
    reads asset-check results sees whether the gate passed, not only the warn
    check. On a collapse the asset has already raised, so the run fails and no
    result is recorded here, which is the correct signal. An undefined rate is
    reported as failed, matching the enforcement.
    """
    rate = metrics.carryover_rate_from_path(str(config.DB))
    passed = rate is not None and rate >= gates.HARD_MIN_CARRYOVER_RATE
    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        metadata={
            "carryover_rate": gates.rate_meta(rate),
            "hard_floor": gates.rate_meta(gates.HARD_MIN_CARRYOVER_RATE),
        },
    )


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
