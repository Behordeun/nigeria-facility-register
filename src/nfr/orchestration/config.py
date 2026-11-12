"""
Where the refresh reads and writes, the run tags it uses, and the fingerprint
of its inputs bound to those paths.

Every other orchestration module reads these through the module (config.DB, not
a copied name), so a test can point the whole refresh at a temporary tree by
patching one attribute.
"""
from __future__ import annotations

from pathlib import Path

from nfr.orchestration import inputs

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
# Previously published registers, kept so a bad release can be rolled back with
# scripts/restore_register.py, and how many to keep.
ARCHIVE_DIR = BUILD_DIR / "published"
ARCHIVE_KEEP = 3
# Written by a restore: the fingerprint of the register that was rolled back.
# While the inputs still fingerprint the same, no refresh publishes them again.
HOLD_FILE = BUILD_DIR / "publish_hold.json"
# The chat webhook alerts are posted to (unset: no alerts), and the record of
# the last alert sent, so a failure retried every half hour alerts once.
ALERT_WEBHOOK_URL_ENV = "NFR_ALERT_WEBHOOK_URL"
ALERT_STATE_FILE = BUILD_DIR / "alert_state.json"
# A check-in URL called after each successful refresh (unset: no heartbeat).
# The service behind it alerts when the weekly check-in stops arriving.
HEARTBEAT_URL_ENV = "NFR_HEARTBEAT_URL"
# The env var the dbt profile reads for its DuckDB path. The orchestrated run
# sets it to the run-scoped side file; unset (a plain make build) it defaults to
# the live DB inside profiles.yml.
DBT_DB_ENV = "NFR_DUCKDB_PATH"
DBT_DIR = ROOT / "dbt"
RAW_GEOPACKAGES = ["nga_health_facilities_v2_0.gpkg", "grid3_nga_health_facilities_v3_0.gpkg"]

# Run tag carrying the raw-input fingerprint a refresh was launched for. The
# vintage sensor reads it back to tell whether a given vintage already SUCCEEDED,
# which is what success-gates the sensor (see sensors.new_vintage_sensor).
VINTAGE_FP_TAG = "nfr/vintage_fingerprint"
# Run tag carrying the population year the sensor fingerprinted, so a run
# launched just before 1 January still builds the year it was launched for.
POPULATION_YEAR_TAG = "nfr/population_year"
# The file in a run's snapshot that records the population year the run builds,
# so the publish step fingerprints and dbt builds the same year.
SNAPSHOT_YEAR_FILE = "population_year.txt"


def staging_db_path(run_id: str) -> Path:
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


def run_snapshot_dir(run_id: str) -> Path:
    """Run-scoped directory holding this run's immutable copy of the raw inputs.

    Derived from the run id so two runs never share a snapshot; the loader reads
    from here, not the live input folders, so a change to them after the run
    starts cannot alter what it builds.
    """
    safe = "".join(c for c in run_id if c.isalnum() or c in "-_") or "manual"
    return SNAPSHOTS_DIR / safe


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
