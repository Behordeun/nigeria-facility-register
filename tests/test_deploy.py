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
    assert result.returncode != 0 and "is not on main" in result.stderr
    assert _git(host, "describe", "--tags") == "v0.1.0"


def test_deploy_refuses_to_move_the_host_backwards(host):
    assert _deploy(host, "v0.2.0").returncode == 0
    result = _deploy(host, "v0.1.0")
    assert result.returncode != 0 and "already past v0.1.0" in result.stderr
    assert _git(host, "describe", "--tags") == "v0.2.0", "rolling the code back is a manual step"


def test_deploy_refuses_when_tracked_files_have_local_changes(host):
    (host / "VERSION").write_text("edited on the host")
    result = _deploy(host, "v0.2.0")
    assert result.returncode != 0 and "local changes" in result.stderr
    assert (host / "VERSION").read_text() == "edited on the host", "local edits must be left alone"


def test_deploy_refuses_a_checkout_off_main(host):
    _git(host, "switch", "-qc", "hotfix")
    result = _deploy(host, "v0.2.0")
    assert result.returncode != 0 and "must be on main" in result.stderr


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
    assert "StrictHostKeyChecking=yes" in run and "UserKnownHostsFile=" in run
    assert re.search(r'"\$DEPLOY_USER@\$DEPLOY_HOST" "\$TAG"\s*$', run), "the only remote argument is the tag"
