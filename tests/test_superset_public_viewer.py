"""Tests for the public, view-only Superset access.

They pin three guarantees without a running Superset: the Public viewer role's
allowlist never includes a permission that exports data, runs SQL, builds
charts, renders server-side downloads or edits the shared account; the
Cloudflare Tunnel container can reach Superset and nothing else; and the
proxy-aware Superset settings apply only when a public URL is configured.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name!r} from {path}; was the file moved?")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pv = _load_module("superset_public_viewer", ROOT / "scripts" / "superset_public_viewer.py")
sd = _load_module("superset_dashboard", ROOT / "scripts" / "superset_dashboard.py")
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

# Permissions the shared account may never hold, by name, whatever view they are on.
_EXPORTING_PERMISSIONS = {"can_csv", "can_export", "can_drill", "can_samples", "can_view_query",
                          "can_view_chart_as_table", "can_cache_dashboard_screenshot",
                          "can_explore", "can_sqllab", "can_execute_sql_query"}
# The one write the dashboard needs: storing the visitor's own filter selection.
_ALLOWED_WRITES = {("can_write", "DashboardFilterStateRestApi")}


def test_viewer_allowlist_excludes_every_forbidden_permission():
    overlap = set(pv.VIEWER_PERMISSIONS) & pv.FORBIDDEN_PERMISSIONS
    assert not overlap, f"the Public viewer role must not grant {sorted(overlap)}"


def test_viewer_allowlist_has_no_export_sql_or_download_permission():
    offending = [pair for pair in pv.VIEWER_PERMISSIONS if pair[0] in _EXPORTING_PERMISSIONS]
    assert not offending, f"expected no export, SQL or download permission, got {offending}"


def test_viewer_allowlist_writes_only_filter_state():
    writes = {pair for pair in pv.VIEWER_PERMISSIONS if pair[0] in {"can_write", "can_add", "can_edit", "can_delete"}}
    assert writes <= _ALLOWED_WRITES, f"unexpected write permissions {sorted(writes - _ALLOWED_WRITES)}"


def test_viewer_allowlist_cannot_reach_sql_lab_or_explore_views():
    views = {view for _, view in pv.VIEWER_PERMISSIONS}
    assert not views & {"SQLLab", "SQL Lab", "Explore", "ExploreFormDataRestApi", "Dataset", "Database"}, (
        f"expected no SQL Lab, Explore, dataset or database view, got {sorted(views)}"
    )


def test_viewer_cannot_change_the_shared_password():
    views = {view for _, view in pv.VIEWER_PERMISSIONS}
    assert not views & {"ResetMyPasswordView", "UserDBModelView"}, "the shared account must not edit itself"


def test_viewer_has_no_blanket_data_access():
    names = {perm for perm, _ in pv.VIEWER_PERMISSIONS}
    assert not names & {"all_datasource_access", "all_database_access", "datasource_access", "schema_access"}, (
        "dataset access must come only from the dashboard's own datasets, added at run time"
    )


def test_viewer_targets_the_bundled_dashboard():
    assert pv.DASHBOARD_UUID == sd.DASHBOARD_UUID, "the role must follow the dashboard the bundle imports"


def test_viewer_spec_never_carries_the_password():
    spec = pv.build_spec()
    assert "password" not in json.dumps(spec).replace("min_password_length", ""), (
        "the password must come from the container environment, not the spec"
    )
    assert spec["username"] == "public-viewer"
    assert spec["min_password_length"] >= 16


def test_viewer_program_carries_the_spec_on_stdin_not_in_the_environment():
    program = pv.build_program()
    first_line = program.splitlines()[0]
    assert first_line.startswith("SPEC_JSON = "), "the spec must be bound inside the program"
    assert json.loads(json.loads(first_line.split(" = ", 1)[1])) == pv.build_spec()
    assert "os.environ[\"NFR_VIEWER_SPEC\"]" not in program


def _networks(service: dict) -> set[str]:
    """Network names of a compose service, whether written as a list or a mapping."""
    return set(service.get("networks") or [])


def test_cloudflared_is_opt_in_and_shares_a_network_with_superset_only():
    services = COMPOSE["services"]
    tunnel = services["cloudflared"]
    assert tunnel.get("profiles") == ["public"], "the tunnel must only start with --profile public"
    assert _networks(tunnel) == {"public"}, "the tunnel must not join the internal network"
    on_public = sorted(name for name, svc in services.items() if "public" in _networks(svc))
    assert on_public == ["cloudflared", "superset"], f"only Superset may share the tunnel's network, got {on_public}"
    assert "ports" not in tunnel, "the tunnel dials out; it must publish no host port"


def test_every_service_comes_back_after_a_host_reboot():
    for name, service in COMPOSE["services"].items():
        assert service.get("restart") == "unless-stopped", (
            f"{name} must restart with Docker after a reboot, or the dashboard stays down"
        )


def test_superset_stops_rather_than_serving_when_its_bootstrap_fails():
    script = COMPOSE["services"]["superset"]["command"][-1]
    assert script.lstrip().startswith("set -e\n"), (
        "a failed db upgrade or init must exit so the restart policy retries it"
    )
    assert "|| true" not in script, "no bootstrap step may hide its failure from set -e"


def test_superset_and_dagster_ports_stay_on_loopback():
    for name in ("superset", "webserver"):
        for port in COMPOSE["services"][name].get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} must publish on 127.0.0.1 only, got {port}"


def _load_config(monkeypatch: pytest.MonkeyPatch, public_url: str | None):
    monkeypatch.setenv("SUPERSET_DB_PASSWORD", "test-only")
    if public_url is None:
        monkeypatch.delenv("SUPERSET_PUBLIC_URL", raising=False)
    else:
        monkeypatch.setenv("SUPERSET_PUBLIC_URL", public_url)
    return _load_module("superset_config_under_test", ROOT / "superset" / "superset_config.py")


def test_local_stack_keeps_plain_http_defaults(monkeypatch: pytest.MonkeyPatch):
    config = _load_config(monkeypatch, None)
    assert not hasattr(config, "ENABLE_PROXY_FIX"), "proxy headers must not be trusted without a proxy"
    assert not hasattr(config, "SESSION_COOKIE_SECURE"), "a local http stack needs a non-secure cookie"


def test_public_url_turns_on_proxy_secure_cookie_and_login_rate_limit(monkeypatch: pytest.MonkeyPatch):
    config = _load_config(monkeypatch, "https://dashboard.example.org/")
    assert config.ENABLE_PROXY_FIX is True
    assert config.PROXY_FIX_CONFIG["x_for"] == 1, "trust exactly one hop for the client address"
    assert config.PROXY_FIX_CONFIG["x_proto"] == 1, "trust exactly one hop for the scheme"
    assert config.SESSION_COOKIE_SECURE is True, "the session cookie must be HTTPS-only when public"
    assert config.SESSION_COOKIE_HTTPONLY is True, "page scripts must not read the session cookie"
    assert config.SESSION_COOKIE_SAMESITE in {"Lax", "Strict"}, "the cookie must not be sent cross-site"
    assert config.PERMANENT_SESSION_LIFETIME.total_seconds() <= 24 * 3600, "public sessions must end within a day"
    assert config.RATELIMIT_ENABLED is True, "the rate limiter must be on when public"
    assert config.AUTH_RATE_LIMITED is True, "sign-in attempts must be rate-limited when public"
    assert config.RATELIMIT_STORAGE_URI.endswith("/4"), "rate-limit counters need their own Redis database"
    assert config.WEBDRIVER_BASEURL_USER_FRIENDLY == "https://dashboard.example.org/"
