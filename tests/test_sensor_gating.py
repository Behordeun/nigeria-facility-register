"""Tests for the vintage sensor's success-gating.

The sensor must fire for a vintage only while no SUCCEEDED run exists for its
fingerprint, so a run that failed is re-fired on the next tick rather than
skipped. These test the pure gate helper with a fake instance, so no Dagster
run storage is needed. dagster is an optional install; skip without it.
"""
import pytest

pytest.importorskip("dagster")

from nfr.orchestration import definitions as d  # noqa: E402


class _FakeInstance:
    """Records the filter it was queried with and returns a canned result."""

    def __init__(self, succeeded_fps: set[str]):
        self._succeeded = succeeded_fps
        self.last_filter = None

    def get_run_records(self, filters=None, limit=None):
        self.last_filter = filters
        fp = (filters.tags or {}).get(d.VINTAGE_FP_TAG)
        # Only "return a record" when the queried fingerprint is one that
        # succeeded AND the query is filtered to SUCCESS status.
        from dagster import DagsterRunStatus

        wants_success = DagsterRunStatus.SUCCESS in (filters.statuses or [])
        if wants_success and fp in self._succeeded:
            return [object()]
        return []


def test_succeeded_vintage_is_reported_built():
    inst = _FakeInstance(succeeded_fps={"abc123"})
    assert d._vintage_has_succeeded(inst, "abc123") is True


def test_unbuilt_vintage_is_not_reported_built():
    inst = _FakeInstance(succeeded_fps={"abc123"})
    # A different fingerprint (e.g. a new vintage, or one whose run failed) has
    # no successful run, so the sensor stays eligible to fire.
    assert d._vintage_has_succeeded(inst, "def456") is False


def test_gate_query_filters_on_success_status_and_fingerprint_tag():
    inst = _FakeInstance(succeeded_fps=set())
    d._vintage_has_succeeded(inst, "zzz999")
    from dagster import DagsterRunStatus

    assert inst.last_filter.tags == {d.VINTAGE_FP_TAG: "zzz999"}
    assert DagsterRunStatus.SUCCESS in inst.last_filter.statuses


def test_vintage_fp_tag_is_stable():
    # The run request and the gate query must agree on the tag key, or the gate
    # would never find the run it launched. Pin the key.
    assert d.VINTAGE_FP_TAG == "nfr/vintage_fingerprint"
