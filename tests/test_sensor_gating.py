"""Tests for the vintage sensor's published-vintage gate and its helpers.

The sensor rebuilds only when the raw-input fingerprint differs from the one the
live database was last built from, and never stacks a request behind a running
refresh. These test the published-fingerprint sentinel and the running/queued
query without a live Dagster daemon. dagster and duckdb are available in the
base suite; skip only if dagster is absent.
"""
import pytest

pytest.importorskip("dagster")

from nfr.orchestration import definitions as d  # noqa: E402
from nfr.orchestration import publish  # noqa: E402


def test_published_fingerprint_round_trips(tmp_path):
    db = str(tmp_path / "live.duckdb")
    publish.write_published_fingerprint(db, "abc123")
    assert publish.read_published_fingerprint(db) == "abc123"


def test_published_fingerprint_is_overwritten_not_appended(tmp_path):
    db = str(tmp_path / "live.duckdb")
    publish.write_published_fingerprint(db, "old")
    publish.write_published_fingerprint(db, "new")
    # Exactly the latest vintage is recorded, so the sensor compares against one
    # value, not an accumulating history.
    assert publish.read_published_fingerprint(db) == "new"


def test_read_published_fingerprint_none_when_db_missing(tmp_path):
    # First run: no live DB yet, so nothing is published and any vintage is new.
    assert publish.read_published_fingerprint(str(tmp_path / "absent.duckdb")) is None


def test_read_published_fingerprint_none_when_sentinel_absent(tmp_path):
    # A DB that exists but predates the sentinel reports no published vintage,
    # rather than erroring, so the sensor treats the first build as new.
    import duckdb

    db = str(tmp_path / "live.duckdb")
    con = duckdb.connect(db)
    con.execute("create table something_else (x int)")
    con.close()
    assert publish.read_published_fingerprint(db) is None


class _FakeInstance:
    def __init__(self, active_count: int):
        self._active = active_count
        self.last_filter = None

    def get_run_records(self, filters=None, limit=None):
        self.last_filter = filters
        return [object()] * min(self._active, (limit or self._active))


def test_running_or_queued_true_when_an_active_run_exists():
    assert d._running_or_queued_refresh(_FakeInstance(active_count=1)) is True


def test_running_or_queued_false_when_none_active():
    assert d._running_or_queued_refresh(_FakeInstance(active_count=0)) is False


def test_running_or_queued_query_filters_to_the_refresh_job_and_active_states():
    from dagster import DagsterRunStatus

    inst = _FakeInstance(active_count=0)
    d._running_or_queued_refresh(inst)
    assert inst.last_filter.job_name == d.refresh_job.name
    # The query must look at non-terminal states only; a terminal status in the
    # set would wrongly count a finished run as still active.
    statuses = set(inst.last_filter.statuses)
    assert DagsterRunStatus.QUEUED in statuses
    assert DagsterRunStatus.STARTED in statuses
    assert DagsterRunStatus.SUCCESS not in statuses
    assert DagsterRunStatus.FAILURE not in statuses


def test_vintage_fp_tag_is_stable():
    assert d.VINTAGE_FP_TAG == "nfr/vintage_fingerprint"
