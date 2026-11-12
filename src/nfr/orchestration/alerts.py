"""
Outbound alerts: post a short message to a chat webhook when something needs a
person.

A failed refresh, a rejected build and an upstream release waiting for review
are otherwise visible only inside Dagster, which nobody watches between weekly
runs. When NFR_ALERT_WEBHOOK_URL is set, the refresh posts one JSON message to
it. The payload carries the text as both "text" and "content", which Slack,
Microsoft Teams (incoming webhook), Mattermost, Google Chat and Discord each
accept, so any of them works without an adapter.

Alerting never changes the outcome of a run: an unset URL does nothing, and a
failed post is reported back as a status string, never raised. The URL is a
credential (whoever holds it can post to the channel), so it is never logged or
echoed, and only https is accepted except for a relay on this machine.

The sensor re-requests a failing vintage every half hour, so a failure is sent
once and then deduplicated by a key recorded in a small state file; the record
is cleared when a refresh publishes, so the next failure alerts again.

A heartbeat covers the opposite case, when nothing runs at all: each successful
refresh calls NFR_HEARTBEAT_URL (a check-in URL such as healthchecks.io), and
that service alerts when the weekly check-in stops arriving. The same rules
apply: unset does nothing, failures are returned, the URL is never shown.
"""
from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 10
MAX_TEXT_CHARS = 1500
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _check_url(url: str) -> None:
    """Accept https, or http only to this machine (a local relay or a test)."""
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError("the URL has no host")
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
        return
    raise ValueError("the URL must use https")


def _deliver(request: urllib.request.Request, timeout: float, receiver: str) -> str:
    """Send one request; return "sent" or why not, never the URL, never raising."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (scheme checked by callers)
            status = response.status
    except urllib.error.HTTPError as exc:
        return f"not sent: the {receiver} answered HTTP {exc.code}"
    except OSError as exc:  # URLError (refused, DNS, timeout) is an OSError
        reason = getattr(exc, "reason", exc)
        return f"not sent: {type(reason).__name__}"
    return "sent" if 200 <= status < 300 else f"not sent: the {receiver} answered HTTP {status}"


def payload(title: str, body: str) -> bytes:
    """The JSON message: a bold title line and the body, trimmed for chat."""
    text = f"*{title}*\n{body}".strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 1] + "…"
    return json.dumps({"text": text, "content": text}).encode()


def send(url: str | None, title: str, body: str, timeout: float = TIMEOUT_SECONDS) -> str:
    """Post one alert; return what happened, never raise.

    The returned status never contains the URL, so it is safe to log and to
    record in run metadata.
    """
    if not url:
        return "not configured"
    try:
        _check_url(url)
    except ValueError as exc:
        return f"not sent: {exc}"
    request = urllib.request.Request(
        url, data=payload(title, body), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "nfr-alerts"},
    )
    return _deliver(request, timeout, "webhook")


def ping(url: str | None, timeout: float = TIMEOUT_SECONDS) -> str:
    """Check in with a heartbeat URL (one GET); return what happened, never raise."""
    if not url:
        return "not configured"
    try:
        _check_url(url)
    except ValueError as exc:
        return f"not sent: {exc}"
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "nfr-alerts"})
    return _deliver(request, timeout, "heartbeat")


def already_sent(state_file: Path, key: str) -> bool:
    """Whether this exact alert was the last one sent since the last publish."""
    try:
        return json.loads(state_file.read_text()).get("last") == key
    except (OSError, ValueError, AttributeError):
        return False


def record_sent(state_file: Path, key: str) -> None:
    """Remember the alert just sent, so a repeat of it is not sent again."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"last": key}) + "\n")


def reset(state_file: Path) -> None:
    """Forget the last alert (after a successful publish)."""
    state_file.unlink(missing_ok=True)


def send_once(url: str | None, state_file: Path, key: str, title: str, body: str) -> str:
    """Send unless this alert was the last one sent; record it when it goes out."""
    if not url:
        return "not configured"
    if already_sent(state_file, key):
        return "already sent"
    status = send(url, title, body)
    if status == "sent":
        # Failing to record only means a repeat may be sent again, which is
        # better than a lost alert, so it never fails the caller.
        with contextlib.suppress(OSError):
            record_sent(state_file, key)
    return status


def root_cause(error) -> str:
    """The first line of the innermost cause of a Dagster SerializableErrorInfo.

    Dagster wraps an asset's exception ("Error occurred while executing op ..."),
    so the useful message, such as the gate's verdict or a held vintage, is the
    innermost cause.
    """
    while getattr(error, "cause", None) is not None:
        error = error.cause
    message = (getattr(error, "message", "") or "").strip()
    return message.splitlines()[0] if message else "no error message recorded"
