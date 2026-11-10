"""
Create or update the view-only "Public viewer" Superset role and the shared
`public-viewer` account that uses it.

The role is built from an explicit allowlist rather than by trimming Gamma, so a
Superset upgrade that adds permissions to Gamma never widens it. It can open the
published dashboard and nothing else:

- it reads the dashboard and its charts, and uses the dashboard's filters;
- it reaches only the datasets this dashboard's charts read (looked up from the
  dashboard by UUID, so the grant follows the bundle);
- it has no SQL Lab, no Explore (no building charts from a dataset), no CSV or
  Excel export, no drill to detail, no "view query", no server-side PDF or image
  download, no dataset or database lists, and no permalinks or tags it could
  create;
- it cannot change its own password or profile, because the account is shared.

Every run replaces the role's permissions with exactly this set and resets the
account's role and password from SUPERSET_PUBLIC_VIEWER_PASSWORD (in .env, passed
to the superset container by docker compose), so rotating the password is
editing .env, `docker compose up -d superset`, then running this again. The
password never passes through this script's arguments or output.

Run it after `make dashboard`, against the running compose stack:

    python scripts/superset_public_viewer.py [--project NAME]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE = "superset"
DASHBOARD_UUID = "435de0dd-d46c-52d5-a5c1-e75ed9f32288"
ROLE_NAME = "Public viewer"
USERNAME = "public-viewer"
# Superset requires a unique email per user; .invalid never resolves.
EMAIL = "public-viewer@nfr.invalid"
MIN_PASSWORD_LENGTH = 16

# (permission, view) pairs granted besides the per-dataset access. Each one is
# needed to open and filter the dashboard:
VIEWER_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("can_read", "Dashboard"),  # the dashboard API: layout, charts, datasets
    ("can_dashboard", "Superset"),  # the /superset/dashboard/<slug>/ page
    ("can_read", "Chart"),  # /api/v1/chart/data, which draws most charts
    ("can_explore_json", "Superset"),  # draws the deck.gl maps (legacy endpoint)
    ("can_read", "DashboardFilterStateRestApi"),  # native filter state
    ("can_write", "DashboardFilterStateRestApi"),
    ("can_read", "DashboardPermalinkRestApi"),  # open a shared permalink
    ("can_dashboard_permalink", "Superset"),
    ("can_log", "Superset"),  # the page's own event log; 403s otherwise
    ("can_get", "MenuApi"),  # the navigation bar
    ("menu_access", "Dashboards"),
)

# Never granted. Listed so the test can prove the allowlist stays clear of them.
FORBIDDEN_PERMISSIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("can_csv", "Superset"),  # CSV and Excel export of chart data
        ("can_export", "Chart"),
        ("can_export", "Dashboard"),
        ("can_drill", "Dashboard"),  # drill to detail shows the rows behind a chart
        ("can_view_chart_as_table", "Dashboard"),
        ("can_view_query", "Dashboard"),
        ("can_samples", "Datasource"),
        ("can_cache_dashboard_screenshot", "Dashboard"),  # server-side PDF/image
        ("can_explore", "Superset"),
        ("can_read", "Explore"),
        ("can_write", "ExploreFormDataRestApi"),
        ("can_read", "Dataset"),
        ("can_read", "Database"),
        ("can_get", "Datasource"),
        ("menu_access", "SQL Lab"),
        ("can_sqllab", "Superset"),
        ("can_execute_sql_query", "SQLLab"),
        ("can_write", "Chart"),
        ("can_write", "Dashboard"),
        ("can_write", "DashboardPermalinkRestApi"),
        ("can_share_dashboard", "Superset"),
        ("can_share_chart", "Superset"),
        ("can_write", "Tag"),
        ("can_this_form_post", "ResetMyPasswordView"),
        ("can_this_form_get", "ResetMyPasswordView"),
        ("resetmypassword", "UserDBModelView"),
        ("can_userinfo", "UserDBModelView"),
        ("all_datasource_access", "all_datasource_access"),
        ("all_database_access", "all_database_access"),
    }
)

# Runs inside the superset container (fed on stdin to `python -`). The spec
# is prepended to it on stdin as a JSON literal, so nothing reaches the exec's
# environment or arguments; the password comes from the container's
# own environment, never from this script.
_PROGRAM = """
import json
import os
import sys

from superset.app import create_app

spec = json.loads(SPEC_JSON)
app = create_app()
with app.app_context():
    from superset import db, security_manager as sm
    from superset.models.dashboard import Dashboard

    admin = os.environ.get("SUPERSET_ADMIN_USERNAME", "admin")
    if spec["username"] == admin:
        sys.exit("refusing to turn the admin account into the public viewer")
    password = os.environ.get("SUPERSET_PUBLIC_VIEWER_PASSWORD", "")
    if len(password) < spec["min_password_length"]:
        sys.exit(
            "SUPERSET_PUBLIC_VIEWER_PASSWORD must be set in .env, at least "
            f"{spec['min_password_length']} characters; then `docker compose up -d superset`"
        )

    dashboard = db.session.query(Dashboard).filter_by(uuid=spec["dashboard_uuid"]).one_or_none()
    if dashboard is None:
        sys.exit("the dashboard is not in this Superset; run `make dashboard` first")
    if not dashboard.published:
        sys.exit("the dashboard is a draft; publish it so viewers can open it")

    wanted = [tuple(pair) for pair in spec["permissions"]]
    wanted += [("datasource_access", ds.perm) for ds in dashboard.datasources]
    grants, missing = [], []
    for perm, view in wanted:
        pv = sm.find_permission_view_menu(perm, view)
        (grants if pv else missing).append(pv or f"{perm} on {view}")
    if missing:
        sys.exit("permissions missing from this Superset (run `superset init`): " + ", ".join(missing))

    role = sm.find_role(spec["role"]) or sm.add_role(spec["role"])
    role.permissions = grants

    user = sm.find_user(username=spec["username"])
    if user is None:
        user = sm.add_user(
            spec["username"], "Public", "Viewer", spec["email"], [role], password=password
        )
        if not user:
            sys.exit(f"could not create the user {spec['username']!r}")
        action = "created"
    else:
        user.roles = [role]
        user.active = True
        sm.reset_password(user.id, password)
        action = "updated"
    db.session.commit()
    print(
        f"role {spec['role']!r}: {len(grants)} permissions "
        f"({len(dashboard.datasources)} datasets); user {spec['username']!r} {action}"
    )
"""


def build_spec() -> dict[str, object]:
    """Return the role and account settings the in-container program applies."""
    return {
        "role": ROLE_NAME,
        "username": USERNAME,
        "email": EMAIL,
        "dashboard_uuid": DASHBOARD_UUID,
        "permissions": [list(pair) for pair in VIEWER_PERMISSIONS],
        "min_password_length": MIN_PASSWORD_LENGTH,
    }


def _compose(project: str | None) -> list[str]:
    cmd = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml")]
    if project:
        cmd += ["-p", project]
    return cmd


def apply(project: str | None) -> int:
    """Apply the role and account in the running superset service.

    Returns the program's exit status; its own message explains a failure.
    """
    cmd = _compose(project) + ["exec", "-T", SERVICE, "python", "-"]
    result = subprocess.run(cmd, input=build_program(), text=True, check=False)
    return result.returncode


def build_program() -> str:
    """Return the in-container program with the spec bound as a JSON string literal."""
    return f"SPEC_JSON = {json.dumps(json.dumps(build_spec()))}\n" + _PROGRAM


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--project", help="docker compose project name, if not the default")
    args = parser.parse_args(argv)
    return apply(args.project)


if __name__ == "__main__":
    sys.exit(main())
