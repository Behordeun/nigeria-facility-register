"""The heartbeat: one check-in after each successful refresh, and none otherwise.

A check-in service alerts when the weekly refresh stops checking in, so the
heartbeat must arrive after a successful refresh, never after a failed one, do
nothing when unset, and never reveal the URL, which is a credential.
"""
from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from dagster import DagsterInstance, build_run_status_sensor_context, job, op

from nfr.orchestration import alerts, config
from nfr.orchestration.sensors import refresh_heartbeat_sensor


@contextmanager
def _check_in(status: int = 200) -> Iterator[tuple[str, list[tuple[str, str]]]]:
    """A local check-in endpoint that answers `status` and records each request."""
    received: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _record(self):
            received.append((self.command, self.path))
            self.send_response(status)
            self.end_headers()

        do_GET = do_POST = _record  # noqa: N815 (http.server naming)

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/ping/check-token", received
    finally:
        server.shutdown()
        server.server_close()


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@op
def _succeeds():
    return 1


@op
def _fails():
    raise RuntimeError("the gate rejected the build")


@job
def _good_run():
    _succeeds()


@job
def _bad_run():
    _fails()


def _sensor_context(run_job):
    instance = DagsterInstance.ephemeral()
    result = run_job.execute_in_process(instance=instance, raise_on_error=False)
    final = next(
        record.dagster_event for record in reversed(instance.all_logs(result.run_id))
        if record.dagster_event is not None and record.dagster_event.event_type_value in ("PIPELINE_SUCCESS",
                                                                                          "PIPELINE_FAILURE")
    )
    return build_run_status_sensor_context(sensor_name=refresh_heartbeat_sensor.name, dagster_instance=instance,
                                           dagster_run=result.dagster_run, dagster_event=final)


def test_an_unset_heartbeat_does_nothing():
    assert alerts.ping(None) == "not configured"
    assert alerts.ping("") == "not configured"


def test_a_heartbeat_is_one_get_to_the_check_in_url():
    with _check_in() as (url, received):
        assert alerts.ping(url) == "sent"
    assert received == [("GET", "/ping/check-token")]


@pytest.mark.parametrize("url", ["http://hc-ping.example.com/abc", "ftp://example.com/x", "not a url"])
def test_a_heartbeat_needs_https_or_a_local_address(url):
    assert alerts.ping(url).startswith("not sent")


def test_a_rejected_heartbeat_is_reported_not_raised():
    with _check_in(status=404) as (url, _):
        assert alerts.ping(url) == "not sent: the heartbeat answered HTTP 404"


def test_an_unreachable_heartbeat_is_reported_without_the_url():
    status = alerts.ping(f"http://127.0.0.1:{_closed_port()}/ping/check-token", timeout=2)
    assert status.startswith("not sent"), status
    assert "check-token" not in status, "the check-in URL is a credential and must never be echoed"


def test_a_successful_refresh_checks_in_once(monkeypatch):
    with _check_in() as (url, received):
        monkeypatch.setenv(config.HEARTBEAT_URL_ENV, url)
        refresh_heartbeat_sensor(_sensor_context(_good_run))
    assert received == [("GET", "/ping/check-token")], "one check-in per successful refresh"


def test_a_failed_refresh_never_checks_in(monkeypatch):
    with _check_in() as (url, received):
        monkeypatch.setenv(config.HEARTBEAT_URL_ENV, url)
        refresh_heartbeat_sensor(_sensor_context(_bad_run))
    assert received == [], "a failed refresh must not count as a sign of life"


def test_the_heartbeat_sensor_is_a_no_op_without_a_url(monkeypatch):
    monkeypatch.delenv(config.HEARTBEAT_URL_ENV, raising=False)
    with _check_in() as (_, received):
        refresh_heartbeat_sensor(_sensor_context(_good_run))
    assert received == []
