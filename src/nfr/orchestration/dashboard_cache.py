"""
Empty Superset's chart-result cache after a new register is published.

Superset caches chart query results in Redis, keyed by the query text, not by
the contents of the DuckDB file the query ran against. Once a refresh swaps a
new register onto the live path, those cached results describe the old one, so
the refresh empties the cache right after the swap.

The cache has a Redis database of its own (see superset/superset_config.py), so
emptying that whole database touches nothing else. Two commands are all this
needs, so they are sent over a plain socket in Redis's wire protocol rather than
through a client library the pipeline would otherwise not use.
"""
from __future__ import annotations

import socket
from urllib.parse import urlsplit

DEFAULT_PORT = 6379
TIMEOUT_SECONDS = 5.0


class DashboardCacheError(RuntimeError):
    """Redis answered a command with something other than OK."""


def parse_cache_url(url: str) -> tuple[str, int, int]:
    """Return (host, port, database) from a URL such as redis://redis:6379/3.

    Raises ValueError for anything else: another scheme, no host, credentials
    (the compose Redis has none, and this client does not authenticate), or no
    database number, since emptying an unnamed database could empty the wrong one.
    """
    parts = urlsplit(url)
    if parts.scheme != "redis" or not parts.hostname:
        raise ValueError(f"expected a redis:// URL with a host, got {url!r}")
    if parts.username or parts.password:
        raise ValueError("credentials in the cache URL are not supported")
    database = parts.path.lstrip("/")
    if not database.isdigit():
        raise ValueError(f"the cache URL must name a database, e.g. redis://redis:6379/3; got {url!r}")
    return parts.hostname, parts.port or DEFAULT_PORT, int(database)


def _encode(*args: str) -> bytes:
    """Encode one command as a Redis protocol array of bulk strings."""
    out = [f"*{len(args)}\r\n".encode()]
    for arg in args:
        data = arg.encode()
        out.append(f"${len(data)}\r\n".encode() + data + b"\r\n")
    return b"".join(out)


def _read_lines(sock: socket.socket, count: int) -> list[bytes]:
    """Read `count` single-line replies (SELECT and FLUSHDB answer with one line each)."""
    buffer = b""
    while buffer.count(b"\r\n") < count:
        chunk = sock.recv(4096)
        if not chunk:
            raise DashboardCacheError("Redis closed the connection before replying")
        buffer += chunk
    return buffer.split(b"\r\n")[:count]


def clear_dashboard_cache(url: str, timeout: float = TIMEOUT_SECONDS) -> None:
    """Empty the Redis database named by `url`.

    Raises ValueError for a malformed URL, OSError when Redis cannot be reached,
    and DashboardCacheError when Redis rejects a command.
    """
    host, port, database = parse_cache_url(url)
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(_encode("SELECT", str(database)) + _encode("FLUSHDB"))
        replies = _read_lines(sock, 2)
    for command, reply in zip(("SELECT", "FLUSHDB"), replies):
        if reply != b"+OK":
            raise DashboardCacheError(f"Redis answered {command} with {reply.decode(errors='replace')}")
