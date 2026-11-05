"""
Published-vintage sentinel: the fingerprint of the raw inputs the live database
was last built from.

The refresh sensor decides whether to rebuild by comparing the current raw-input
fingerprint to the one recorded here, not by querying Dagster run history. That
makes the LIVE DATABASE the authority on what is published: any launch path that
promotes a build (the sensor, the weekly schedule, a manual run) records the
fingerprint it published, so the sensor sees it regardless of how it ran; a
queued or failed run records nothing, so it never suppresses a rebuild; and a
restored older vintage differs from the published one, so it is rebuilt rather
than skipped while stale data stays live.

The sentinel is written in the same atomic promote step as the marts, into the
side database before it is renamed onto the live path, so the published
fingerprint and the data it describes are always consistent.
"""
from __future__ import annotations

import duckdb

_TABLE = "nfr_published_vintage"


def write_published_fingerprint(db_path: str, fingerprint: str) -> None:
    """Record, in the given database, the raw-input fingerprint it was built
    from. Called against the side database before it is promoted."""
    con = duckdb.connect(db_path)
    try:
        con.execute(f"create table if not exists {_TABLE} (fingerprint varchar not null)")
        con.execute(f"delete from {_TABLE}")
        con.execute(f"insert into {_TABLE} values (?)", [fingerprint])
    finally:
        con.close()


def read_published_fingerprint(db_path: str) -> str | None:
    """Return the fingerprint the live database was built from, or None if the
    database or the sentinel is absent (a first run, before anything published)."""
    import os

    if not os.path.exists(db_path):
        return None
    con = duckdb.connect(db_path, read_only=True)
    try:
        exists = con.execute(
            "select 1 from information_schema.tables where table_name = ?",
            [_TABLE],
        ).fetchone()
        if not exists:
            return None
        row = con.execute(f"select fingerprint from {_TABLE} limit 1").fetchone()
        return row[0] if row else None
    finally:
        con.close()
