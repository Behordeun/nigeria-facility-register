"""Outbound alerts: the payload, the URL rules, failure handling and dedupe.

An alert must reach any common chat webhook, must never fail or slow a run
beyond its timeout, must never reveal the webhook URL, and must not repeat the
same failure every time the sensor retries it.
"""
from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from nfr.orchestration import alerts


@contextmanager
def _webhook(status: int = 200) -> Iterator[tuple[str, list[dict]]]:
    """A local webhook that answers `status` and records the JSON bodies it gets."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (http.server naming)
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(status)
            self.end_headers()

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hook", received
    finally:
        server.shutdown()
        server.server_close()


def test_payload_works_for_slack_and_discord_alike():
    body = json.loads(alerts.payload("Title", "line one"))
    assert body["text"] == body["content"], "Slack reads text, Discord reads content"
    assert body["text"].startswith("*Title*\nline one")


def test_payload_is_trimmed_for_chat():
    body = json.loads(alerts.payload("T", "x" * 5000))
    assert len(body["text"]) <= alerts.MAX_TEXT_CHARS


def test_an_unset_url_sends_nothing():
    assert alerts.send(None, "t", "b") == "not configured"
    assert alerts.send("", "t", "b") == "not configured"


def test_a_webhook_receives_the_alert():
    with _webhook() as (url, received):
        assert alerts.send(url, "Refresh failed", "Cause: gate") == "sent"
    assert len(received) == 1
    assert "Refresh failed" in received[0]["text"]


@pytest.mark.parametrize("url", ["http://hooks.example.com/abc", "ftp://example.com/x", "not a url"])
def test_only_https_or_a_local_relay_is_accepted(url):
    status = alerts.send(url, "t", "b")
    assert status.startswith("not sent"), status


def test_a_rejecting_webhook_is_reported_not_raised():
    with _webhook(status=500) as (url, _):
        assert alerts.send(url, "t", "b") == "not sent: the webhook answered HTTP 500"


def _closed_port() -> int:
    """A local port that was free a moment ago, so nothing is listening on it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_an_unreachable_webhook_is_reported_without_the_url():
    url = f"http://127.0.0.1:{_closed_port()}/secret-token-path"
    status = alerts.send(url, "t", "b", timeout=2)
    assert status.startswith("not sent"), status
    assert "secret-token-path" not in status, "the webhook URL is a credential and must never be echoed"


def test_the_same_failure_is_sent_once_until_reset(tmp_path):
    state = tmp_path / "alert_state.json"
    with _webhook() as (url, received):
        assert alerts.send_once(url, state, "fail|fp1|gate", "t", "b") == "sent"
        assert alerts.send_once(url, state, "fail|fp1|gate", "t", "b") == "already sent"
        assert alerts.send_once(url, state, "fail|fp2|gate", "t", "b") == "sent", "a different failure alerts"
        alerts.reset(state)
        assert alerts.send_once(url, state, "fail|fp2|gate", "t", "b") == "sent", "a reset re-arms the alert"
    assert len(received) == 3


def test_a_failed_send_is_retried_on_the_next_failure(tmp_path):
    # Only an alert that went out is recorded, so one that failed to send is
    # tried again when the sensor reports the failure again.
    state = tmp_path / "alert_state.json"
    with _webhook(status=503) as (url, _):
        assert alerts.send_once(url, state, "k", "t", "b").startswith("not sent")
    assert not alerts.already_sent(state, "k")


def test_an_unreadable_state_file_does_not_block_alerts(tmp_path):
    state = tmp_path / "alert_state.json"
    state.write_text("not json")
    assert not alerts.already_sent(state, "k")


def test_root_cause_reads_the_innermost_error():
    inner = SimpleNamespace(message="RuntimeError: vintage abc is held after a rollback\ndetail", cause=None)
    outer = SimpleNamespace(message="Error occurred while executing op raw_sources", cause=inner)
    assert alerts.root_cause(outer) == "RuntimeError: vintage abc is held after a rollback"


def test_root_cause_without_a_message():
    assert alerts.root_cause(SimpleNamespace(message="", cause=None)) == "no error message recorded"
