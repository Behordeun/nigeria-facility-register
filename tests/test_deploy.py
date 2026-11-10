"""Tests for the release deploy: the host script's checkout stage and the workflow.

The host script is run for real against a throwaway origin and clone. Each tagged
commit carries a stub scripts/deploy_host.sh that only records it was called, so
the tests prove the first stage moves the checkout and hands over to the tag's
own copy without building or starting anything.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_host.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
STUB = '#!/usr/bin/env bash\necho "stage two ran $1 $2 from $(git -C "$(dirname "$0")" describe --tags)"\n'
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **_GIT_ENV},
                          check=True, capture_output=True, text=True).stdout.strip()


def _commit_release(work: Path, tag: str) -> None:
    script = work / "scripts" / "deploy_host.sh"
    script.parent.mkdir(exist_ok=True)
    script.write_text(STUB)
    script.chmod(0o755)
    (work / "VERSION").write_text(tag)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", tag)
    _git(work, "tag", "-a", tag, "-m", tag)
    _git(work, "push", "-q", "origin", "main", tag)


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A host checkout at v0.1.0 whose origin also has v0.2.0 on main."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "switch", "-qc", "main")
    _commit_release(work, "v0.1.0")
    checkout = tmp_path / "host"
    _git(tmp_path, "clone", "-q", "-b", "main", str(origin), str(checkout))
    _commit_release(work, "v0.2.0")
    return checkout


def _deploy(checkout: Path, ssh_command: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **_GIT_ENV, "NFR_APP_DIR": str(checkout), "SSH_ORIGINAL_COMMAND": ssh_command}
    return subprocess.run(["bash", str(DEPLOY_SCRIPT)], env=env, capture_output=True, text=True)


def test_deploy_fast_forwards_main_and_hands_over_to_the_tags_own_script(host):
    result = _deploy(host, "v0.2.0")
    assert result.returncode == 0, result.stderr
    assert "stage two ran --apply v0.2.0 from v0.2.0" in result.stdout, (
        "the second stage must be the tag's copy of the script, given the tag"
    )
    assert _git(host, "describe", "--tags") == "v0.2.0", "main must now sit on the tag"
    assert _git(host, "symbolic-ref", "--short", "HEAD") == "main", "the checkout must stay on main"


@pytest.mark.parametrize("command", ["main", "v0.2", "v0.2.0; rm -rf ~", "", "--apply v0.2.0"])
def test_deploy_rejects_anything_but_a_release_tag(host, command):
    result = _deploy(host, command)
    assert result.returncode != 0, f"{command!r} must be refused"
    assert "expected a release tag" in result.stderr
    assert _git(host, "describe", "--tags") == "v0.1.0", "a refused deploy must not move the checkout"


def test_deploy_refuses_a_tag_that_is_not_on_main(host, tmp_path):
    work = tmp_path / "work"
    _git(work, "switch", "-qc", "side")
    (work / "side.txt").write_text("x")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "side")
    _git(work, "tag", "-a", "v9.9.9", "-m", "side")
    _git(work, "push", "-q", "origin", "v9.9.9")
    result = _deploy(host, "v9.9.9")
    assert result.returncode != 0, "the deploy must be refused"
    assert "is not on main" in result.stderr
    assert _git(host, "describe", "--tags") == "v0.1.0"


def test_deploy_refuses_to_move_the_host_backwards(host):
    assert _deploy(host, "v0.2.0").returncode == 0
    result = _deploy(host, "v0.1.0")
    assert result.returncode != 0, "the deploy must be refused"
    assert "already past v0.1.0" in result.stderr
    assert _git(host, "describe", "--tags") == "v0.2.0", "rolling the code back is a manual step"


def test_deploy_refuses_when_tracked_files_have_local_changes(host):
    (host / "VERSION").write_text("edited on the host")
    result = _deploy(host, "v0.2.0")
    assert result.returncode != 0, "the deploy must be refused"
    assert "local changes" in result.stderr
    assert (host / "VERSION").read_text() == "edited on the host", "local edits must be left alone"


def test_deploy_refuses_a_checkout_off_main(host):
    _git(host, "switch", "-qc", "hotfix")
    result = _deploy(host, "v0.2.0")
    assert result.returncode != 0, "the deploy must be refused"
    assert "must be on main" in result.stderr


def test_redeploying_the_current_tag_runs_the_second_stage_again(host):
    assert _deploy(host, "v0.2.0").returncode == 0
    result = _deploy(host, "v0.2.0")
    assert result.returncode == 0, result.stderr
    assert "stage two ran --apply v0.2.0" in result.stdout


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_runs_on_release_and_by_hand_with_no_token_permissions():
    workflow = _workflow()
    triggers = workflow[True]  # YAML reads the bare key `on` as true
    assert triggers["release"]["types"] == ["published"]
    assert "tag" in triggers["workflow_dispatch"]["inputs"]
    assert workflow["permissions"] == {}, "the deploy job needs no repository token permissions"
    assert workflow["concurrency"]["cancel-in-progress"] is False, "a running deploy must never be cancelled"
    assert workflow["jobs"]["deploy"]["environment"] == "production", "secrets live on the production environment"


def test_workflow_keeps_expressions_out_of_the_shell_script():
    for step in _workflow()["jobs"]["deploy"]["steps"]:
        assert "${{" not in step.get("run", ""), "untrusted values must reach the shell through env only"


def test_workflow_verifies_the_host_key():
    run = _workflow()["jobs"]["deploy"]["steps"][-1]["run"]
    assert "StrictHostKeyChecking=yes" in run, "an unknown host key must be refused"
    assert "UserKnownHostsFile=" in run, "the host key must come from DEPLOY_KNOWN_HOSTS"
    assert re.search(r'"\$DEPLOY_USER@\$DEPLOY_HOST" "\$TAG"\s*$', run), "the only remote argument is the tag"


# The apply stage, with docker, make, curl and sleep replaced by stand-ins that
# record each call, so the health wait and step order are checked without a stack.
FAKE_DOCKER = r'''#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
# Each lookup returns the next line of its state file, then keeps the last one.
next_line() {
  local file=$1 n
  n=$(cat "$file.n" 2>/dev/null || echo 0)
  echo $((n + 1)) > "$file.n"
  sed -n "$((n + 1))p" "$file" | grep . || tail -n 1 "$file"
}
case "$1 $2" in
  "compose ps") next_line "$STATE/containers" ;;
  "compose exec") exit 1 ;;
  "inspect -f") next_line "$STATE/health.$4" ;;
esac
'''
RECORDER = '#!/usr/bin/env bash\necho "$(basename "$0") $*" >> "$CALLS"\n'


def _apply(tmp_path: Path, containers: list[str], health: list[str]) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    app, bin_dir, state = tmp_path / "app", tmp_path / "bin", tmp_path / "state"
    for d in (app, bin_dir, state):
        d.mkdir()
    (app / ".env").write_text("")
    (state / "containers").write_text("\n".join(containers) + "\n")
    for container in containers:
        statuses = [h.split("=", 1)[1] for h in health if h.startswith(f"{container}=")]
        (state / f"health.{container}").write_text("\n".join(statuses) + "\n")
    for name, body in {"docker": FAKE_DOCKER, "make": RECORDER, "curl": RECORDER, "sleep": RECORDER}.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    calls = tmp_path / "calls"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "NFR_APP_DIR": str(app),
           "CALLS": str(calls), "STATE": str(state)}
    # The stand-in sleep returns at once, so a wait that never ends would spin;
    # the timeout turns that into a failure instead of a hung run.
    result = subprocess.run(["bash", str(DEPLOY_SCRIPT), "--apply", "v0.2.0"], env=env,
                            capture_output=True, text=True, timeout=60)
    return result, calls.read_text().splitlines() if calls.exists() else []


def test_apply_follows_a_container_replaced_while_starting(tmp_path):
    result, calls = _apply(tmp_path, ["old", "new"], ["old=starting", "new=healthy"])
    assert result.returncode == 0, result.stderr
    assert "docker inspect -f {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} new" in calls, (
        "the health wait must look the container up again on each pass"
    )


def test_apply_waits_for_superset_before_importing_and_checks_both_uis(tmp_path):
    result, calls = _apply(tmp_path, ["c1"], ["c1=starting", "c1=healthy"])
    assert result.returncode == 0, result.stderr
    steps = [c for c in calls if c.startswith(("docker compose build", "docker compose up", "make", "curl"))]
    assert steps[:2] == ["docker compose build", "docker compose up -d"]
    assert "8088/health" in steps[2], "Superset must answer before the dashboard is imported"
    assert steps[3] == "make dashboard PY=python3", "the dashboard import follows the Superset check"
    assert "3000/server_info" in steps[-1], "the Dagster UI is checked last"
    assert not any(c.startswith("make public-viewer") for c in calls), "no viewer password, no viewer step"


def test_apply_falls_back_to_the_url_check_without_a_healthcheck(tmp_path):
    result, calls = _apply(tmp_path, ["c1"], ["c1=none"])
    assert result.returncode == 0, result.stderr
    assert "no healthcheck" in result.stdout
    assert any("8088/health" in c for c in calls), "the URL check must stand in for the healthcheck"


def test_apply_stops_before_importing_when_superset_is_unhealthy(tmp_path):
    result, calls = _apply(tmp_path, ["c1"], ["c1=unhealthy"])
    assert result.returncode != 0, "the deploy must be refused"
    assert "unhealthy" in result.stderr
    assert not any(c.startswith("make") for c in calls), "nothing is imported into an unhealthy Superset"
