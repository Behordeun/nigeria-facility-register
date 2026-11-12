"""The heartbeat: one check-in after each successful refresh, and none otherwise.

A check-in service alerts when the weekly refresh stops checking in, so the
heartbeat must arrive after a successful refresh, never after a failed one, do
nothing when unset, and never reveal the URL, which is a credential: not in a
status, not through a redirect, not in an error.
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

CHECK_PATH = "/ping/check-token"
TOKEN = "check-token"


@contextmanager
def _check_in(status: int = 200, location: str | None = None,
              raw_answer: bytes | None = None) -> Iterator[tuple[str, list[tuple[str, str]]]]:
    """A local check-in endpoint that records each request.

    It answers `status` (with a Location header when `location` is given), or
    writes `raw_answer` as-is to stand in for a malformed response.
    """
    received: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _record(self):
            received.append((self.command, self.path))
            if raw_answer is not None:
                self.wfile.write(raw_answer)
                return
            self.send_response(status)
            if location is not None:
                self.send_header("Location", location)
            self.end_headers()

        do_GET = do_POST = _record  # noqa: N815 (http.server naming)

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}{CHECK_PATH}", received
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _refusing_port() -> Iterator[int]:
    """A local port held bound but not listening, so connecting to it is refused.

    The socket stays bound for the whole block, so no other process can take the
    port between choosing it and using it.
    """
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        yield held.getsockname()[1]


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
    assert received == [("GET", CHECK_PATH)]


def test_plain_http_to_another_host_is_refused_before_any_request():
    assert alerts.ping("http://hc-ping.example.com/abc") == "not sent: the URL must use https"


@pytest.mark.parametrize("url", ["ftp://example.com/x", "not a url"])
def test_a_heartbeat_needs_an_https_url_with_a_host(url):
    assert alerts.ping(url).startswith("not sent: the URL"), "the URL is refused before any request"


def test_a_url_with_a_pasted_newline_is_refused_without_echoing_it():
    status = alerts.ping(f"https://hc-ping.example.com/{TOKEN}\n")
    assert status == "not sent: the URL contains spaces or control characters"


def test_a_url_that_cannot_be_parsed_is_refused_without_quoting_it():
    # A fullwidth '#' in the host part makes urlsplit raise an error that
    # quotes the host part, credential included.
    status = alerts.ping(f"https://user:{TOKEN}＃@hc-ping.example.com/abc")
    assert status == "not sent: the URL is malformed"
    assert TOKEN not in status


def test_a_redirect_is_reported_not_followed():
    with _check_in() as (elsewhere, followed), _check_in(status=302, location=elsewhere) as (url, _):
        status = alerts.ping(url)
    assert status == "not sent: the heartbeat answered HTTP 302"
    assert followed == [], "a redirect must not carry the URL anywhere it did not point"


def test_a_malformed_answer_is_reported_without_the_url():
    with _check_in(raw_answer=b"not an http response\r\n\r\n") as (url, _):
        status = alerts.ping(url, timeout=5)
    assert status.startswith("not sent"), status
    assert TOKEN not in status


def test_a_rejected_heartbeat_is_reported_not_raised():
    with _check_in(status=404) as (url, _):
        assert alerts.ping(url) == "not sent: the heartbeat answered HTTP 404"


def test_an_unreachable_heartbeat_is_reported_without_the_url():
    with _refusing_port() as port:
        status = alerts.ping(f"http://127.0.0.1:{port}{CHECK_PATH}", timeout=2)
    assert status.startswith("not sent"), status
    assert TOKEN not in status, "the check-in URL is a credential and must never be echoed"


def test_a_successful_refresh_checks_in_once(monkeypatch):
    with _check_in() as (url, received):
        monkeypatch.setenv(config.HEARTBEAT_URL_ENV, url)
        refresh_heartbeat_sensor(_sensor_context(_good_run))
    assert received == [("GET", CHECK_PATH)], "one check-in per successful refresh"


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
