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

import os

import duckdb

# The sentinel table name. Used only as a bind-parameter VALUE in the
# information_schema lookup; every statement that names it as an identifier
# below is a fully literal constant, so no value is ever concatenated into SQL.
_TABLE = "nfr_published_vintage"

# Fully literal statements: the table name is a fixed identifier spelled inline,
# never interpolated or concatenated, so each string handed to execute() is a
# compile-time constant with no string-to-SQL path. The fingerprint is passed as
# a bind parameter, never formatted into the statement.
_CREATE = 'create table if not exists "nfr_published_vintage" (fingerprint varchar not null)'
_DELETE = 'delete from "nfr_published_vintage"'
_INSERT = 'insert into "nfr_published_vintage" values (?)'
_SELECT = 'select fingerprint from "nfr_published_vintage" limit 1'


def write_published_fingerprint(db_path: str, fingerprint: str) -> None:
    """Record, in the given database, the raw-input fingerprint it was built
    from. Called against the side database before it is promoted."""
    con = duckdb.connect(db_path)
    try:
        con.execute(_CREATE)
        con.execute(_DELETE)
        con.execute(_INSERT, [fingerprint])
    finally:
        con.close()


def read_published_fingerprint(db_path: str) -> str | None:
    """Return the fingerprint the live database was built from, or None if the
    database or the sentinel is absent (a first run, before anything published)."""
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
        row = con.execute(_SELECT).fetchone()
        return row[0] if row else None
    finally:
        con.close()
