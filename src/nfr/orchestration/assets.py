"""
The refresh's two assets (load the snapshotted inputs, then build, gate and
publish the register) and the monthly upstream-release watch.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from dagster import MetadataValue, asset

from nfr.orchestration import config, gates, inputs, metrics, snapshot, upstream

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
    if missing := [n for n in config.RAW_GEOPACKAGES if not (config.RAW / n).exists()]:
        raise RuntimeError(
            f"missing raw inputs {missing}; fetch them with scripts/fetch_sources.py "
            "(HDX serves them behind a WAF, so the download is manual)"
        )
    if not (config.DERIVED / inputs.POPULATION_TABLE).is_file():
        raise RuntimeError(f"missing {config.DERIVED / inputs.POPULATION_TABLE}; the build needs the population table")

    # Immutable per-run snapshot of the inputs; the loader reads from here.
    # Everything after the snapshot is created runs under try/except, so ANY
    # failure (a tag mismatch, the loader, the count read) clears the snapshot
    # rather than leaving it under build/snapshots/ for a run that will not reach
    # dbt_models' cleanup. dbt_models re-creates nothing: on a successful
    # raw_sources the snapshot is left in place for it to read and then clear.
    snapshot_dir = config.run_snapshot_dir(context.run_id)
    snapshot.create_snapshot(config.RAW, snapshot_dir, config.RAW_GEOPACKAGES)
    try:
        snapshot.create_snapshot(config.FRAME, snapshot_dir / inputs.FRAME_SUBDIR, inputs.input_files(config.FRAME))
        snapshot.create_snapshot(config.DERIVED, snapshot_dir / inputs.DERIVED_SUBDIR, inputs.input_files(config.DERIVED))
        population_year = _launch_population_year(context, snapshot_dir)
        (snapshot_dir / config.SNAPSHOT_YEAR_FILE).write_text(str(population_year))
        snapshot_fp = config.snapshot_fingerprint(snapshot_dir, population_year)

        # If the sensor launched this run for a specific vintage, confirm the
        # snapshot is that vintage. A mismatch means the inputs changed between
        # the sensor fingerprinting it and this snapshot, so the launched vintage
        # is no longer on disk; fail rather than publish the wrong bytes under
        # the requested fingerprint. The next sensor tick builds the new vintage.
        requested_fp = context.run.tags.get(config.VINTAGE_FP_TAG) if context.run else None
        if requested_fp and requested_fp != snapshot_fp:
            raise RuntimeError(
                f"inputs changed since launch: run requested vintage {requested_fp} "
                f"but the inputs now fingerprint as {snapshot_fp}. Not publishing; the next "
                "sensor tick will build the current vintage."
            )

        # Build into a run-scoped side file, never the live DB. A stale file from
        # a crashed earlier run with this id is removed first so the load starts
        # clean.
        staging_db = config.staging_db_path(context.run_id)
        config.STAGING_DIR.mkdir(parents=True, exist_ok=True)
        staging_db.unlink(missing_ok=True)

        # Load from the SNAPSHOT (--raw), not the live directory. sys.executable,
        # not a bare "python", so the subprocess runs this interpreter/venv.
        subprocess.run(
            [sys.executable, "-m", "nfr.load_sources", "--db", str(staging_db),
             "--raw", str(snapshot_dir),
             "--frame", str(snapshot_dir / inputs.FRAME_SUBDIR),
             "--derived", str(snapshot_dir / inputs.DERIVED_SUBDIR)],
            cwd=str(config.ROOT), check=True,
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


@asset(deps=[raw_sources])
def dbt_models(context) -> None:
    """Run the dbt staging and marts build, enforce the quality gate, then emit
    the run's metrics.

    The hard carry-over gate runs here, not as a blocking asset check: raising
    in the asset fails the refresh job, where a check downstream of a terminal
    asset would not. The dbt tests (referential integrity, row-count bounds)
    already fail the build inside `dbt build` above.
    """
    staging_db = config.staging_db_path(context.run_id)
    if not staging_db.is_file():
        raise RuntimeError(
            f"staging database {staging_db} is missing; raw_sources must run first"
        )

    # Fingerprint the SNAPSHOT this build is made from, not the live inputs:
    # raw_sources loaded the snapshot, which is immutable for the run, so this is
    # exactly the vintage being published even if the inputs have since changed. It
    # is recorded in the side database before promotion so the live DB always
    # knows which raw vintage it holds (the sensor reads this, not run history).
    snapshot_dir = config.run_snapshot_dir(context.run_id)
    if not snapshot_dir.is_dir():
        raise RuntimeError(
            f"snapshot {snapshot_dir} is missing; raw_sources must run first"
        )
    population_year = int((snapshot_dir / config.SNAPSHOT_YEAR_FILE).read_text())
    built_fingerprint = config.snapshot_fingerprint(snapshot_dir, population_year)

    try:
        # Build dbt against the SIDE database, not the live one: the profile
        # reads NFR_DUCKDB_PATH. dbt build runs the models and the data-quality
        # tests, so a referential or row-count failure aborts here before any
        # promote. sys.executable keeps the subprocess on this venv's dbt.
        env = {**os.environ, config.DBT_DB_ENV: str(staging_db)}
        # `python -m dbt.cli.main`, not `-m dbt`: dbt ships no dbt.__main__, so
        # `-m dbt` fails with "No module named dbt.__main__". Running the cli
        # module keeps the subprocess on this interpreter's venv dbt.
        subprocess.run(
            [sys.executable, "-m", "dbt.cli.main", "build", "--profiles-dir", ".",
             "--vars", f"{{population_year: {population_year}}}"],
            cwd=str(config.DBT_DIR), check=True, env=env,
        )
        # Enforce the hard carry-over gate against the staged build. Raises if
        # the rate does not clear the floor, so the side file is never promoted.
        gates.enforce_carryover_gate(str(staging_db))
        m = metrics.collect_from_path(str(staging_db))
    except BaseException:
        # Any failure (dbt build, a test, the gate, a cancellation) discards the
        # staged register and the run's snapshot, and leaves the live DB untouched.
        staging_db.unlink(missing_ok=True)
        snapshot.clear_snapshot(snapshot_dir)
        raise

    gates.promote(staging_db, built_fingerprint, snapshot_dir)
    dashboard_cache_status = gates.clear_dashboard_cache(context.log)
    context.add_output_metadata(
        {
            "dashboard_cache": dashboard_cache_status,
            "db": str(config.DB),
            "promoted_from": str(staging_db),
            "published_fingerprint": built_fingerprint,
            "authority_rows": m.authority_rows,
            "lineage_rows": m.lineage_rows,
            "coverage_lga_resolve_rate": gates.rate_meta(m.coverage_lga_resolve_rate),
            "drift_lga_resolve_rate": gates.rate_meta(m.drift_lga_resolve_rate),
            "carryover_rate": gates.rate_meta(m.carryover_rate),
        }
    )

    # The snapshot has served its purpose (loaded and published); remove it so
    # snapshots do not accumulate under build/snapshots/ run after run.
    snapshot.clear_snapshot(snapshot_dir)


def _launch_population_year(context, snapshot_dir: Path) -> int:
    """The year the run was launched for (the sensor's tag), else today's.

    A schedule or manual run carries no tag, so it builds the year a build made
    today describes, chosen from the snapshot's own population table.
    """
    tagged = context.run.tags.get(config.POPULATION_YEAR_TAG) if context.run else None
    if tagged:
        return int(tagged)
    table = snapshot_dir / inputs.DERIVED_SUBDIR / inputs.POPULATION_TABLE
    return inputs.select_population_year(table, date.today())


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
