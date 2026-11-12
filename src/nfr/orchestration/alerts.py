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
echoed, only https is accepted (plain http only to localhost, for tests), and a
redirect is never followed, so the URL only goes where it points.

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
import http.client
import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 10
MAX_TEXT_CHARS = 1500
NOT_CONFIGURED = "not configured"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Treat a redirect as the answer instead of following it.

    Following one would send the URL, token and all, to a destination that
    _check_url never saw, possibly over plain http. Refused, the 3xx is
    reported like any other non-2xx answer.
    """

    def redirect_request(self, *_args, **_kwargs):
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def _check_url(url: str) -> None:
    """Accept https, or http only to this machine (a local relay or a test)."""
    # A pasted URL can carry a trailing newline; http.client would reject it
    # with an error that quotes the URL, so it is refused here first.
    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError("the URL contains spaces or control characters")
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError("the URL has no host")
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
        return
    raise ValueError("the URL must use https")


def _deliver(url: str | None, receiver: str, timeout: float, data: bytes | None = None,
             headers: dict[str, str] | None = None) -> str:
    """Send one request (a POST with `data`, else a GET); return what happened.

    Never raises and never includes the URL: the result is "not configured",
    "sent", or "not sent: <reason>", safe to log and to record in metadata.
    """
    if not url:
        return NOT_CONFIGURED
    try:
        _check_url(url)
    except ValueError as exc:
        return f"not sent: {exc}"
    try:
        request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                         headers={"User-Agent": "nfr-alerts", **(headers or {})})
        with _OPENER.open(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        return f"not sent: the {receiver} answered HTTP {exc.code}"
    except OSError as exc:  # URLError (refused, DNS, timeout) is an OSError
        reason = getattr(exc, "reason", exc)
        return f"not sent: {type(reason).__name__}"
    except (ValueError, http.client.HTTPException) as exc:  # a malformed URL or a malformed answer
        return f"not sent: {type(exc).__name__}"
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
    return _deliver(url, "webhook", timeout, data=payload(title, body),
                    headers={"Content-Type": "application/json"})


def ping(url: str | None, timeout: float = TIMEOUT_SECONDS) -> str:
    """Check in with a heartbeat URL (one GET); return what happened, never raise."""
    return _deliver(url, "heartbeat", timeout)


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
        return NOT_CONFIGURED
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
