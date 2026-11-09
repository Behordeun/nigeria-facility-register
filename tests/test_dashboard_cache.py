"""Clearing Superset's chart-result cache after a publish."""
from __future__ import annotations

import socket
import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from nfr.orchestration import dashboard_cache
from nfr.orchestration import config, gates


class _FakeRedis(socketserver.BaseRequestHandler):
    """Answer every command with the server's configured replies; record the bytes received."""

    def handle(self) -> None:
        received = b""
        while received.count(b"\r\n") < 7:  # SELECT n (5 lines) + FLUSHDB (2 lines)
            chunk = self.request.recv(4096)
            if not chunk:
                break
            received += chunk
        self.server.received.append(received)  # type: ignore[attr-defined]
        self.request.sendall(self.server.replies)  # type: ignore[attr-defined]


@contextmanager
def fake_redis(replies: bytes = b"+OK\r\n+OK\r\n") -> Iterator[socketserver.TCPServer]:
    server = socketserver.TCPServer(("127.0.0.1", 0), _FakeRedis)
    server.received = []  # type: ignore[attr-defined]
    server.replies = replies  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _url(server: socketserver.TCPServer, database: int = 3) -> str:
    host, port = server.server_address[:2]
    return f"redis://{host}:{port}/{database}"


def test_clear_selects_the_named_database_then_flushes_only_it() -> None:
    with fake_redis() as server:
        dashboard_cache.clear_dashboard_cache(_url(server, 3))
    assert server.received == [b"*2\r\n$6\r\nSELECT\r\n$1\r\n3\r\n*1\r\n$7\r\nFLUSHDB\r\n"], (
        "expected exactly SELECT 3 then FLUSHDB, so no other Redis database is touched"
    )


def test_clear_raises_when_redis_rejects_the_database() -> None:
    with fake_redis(b"-ERR DB index is out of range\r\n+OK\r\n") as server:
        with pytest.raises(dashboard_cache.DashboardCacheError, match="SELECT"):
            dashboard_cache.clear_dashboard_cache(_url(server))


@pytest.mark.parametrize(
    "url",
    [
        "http://redis:6379/3",
        "redis://redis:6379",
        "redis://redis:6379/cache",
        "redis://user:secret@redis:6379/3",
        "redis:///3",
    ],
)
def test_cache_url_without_a_plain_host_and_database_is_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        dashboard_cache.parse_cache_url(url)


def test_cache_url_defaults_the_port() -> None:
    assert dashboard_cache.parse_cache_url("redis://redis/3") == ("redis", 6379, 3)


class _Log:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)


def test_refresh_skips_the_clear_when_no_cache_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.DASHBOARD_CACHE_URL_ENV, raising=False)
    log = _Log()
    assert gates.clear_dashboard_cache(log) == "not configured"
    assert log.warnings == [], "an unconfigured cache is the dev loop, not a problem to warn about"


def test_refresh_reports_a_cleared_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _Log()
    with fake_redis() as server:
        monkeypatch.setenv(config.DASHBOARD_CACHE_URL_ENV, _url(server))
        assert gates.clear_dashboard_cache(log) == "cleared"
    assert log.warnings == []


def test_unreachable_cache_warns_instead_of_failing_the_published_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A port that was just free: nothing listens there, so the connect is refused.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv(config.DASHBOARD_CACHE_URL_ENV, f"redis://127.0.0.1:{port}/3")
    log = _Log()
    status = gates.clear_dashboard_cache(log)
    assert status.startswith("not cleared:"), f"expected a not-cleared status, got {status!r}"
    assert len(log.warnings) == 1 and "previous figures" in log.warnings[0]
