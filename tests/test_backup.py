"""Tests for the off-host backup script (scripts/backup_host.sh).

The script runs for real, with docker replaced by a stand-in that serves a fake
register and database dump, and a local HTTP server standing in for the
Object Storage pre-authenticated request (PAR). The tests check what is
uploaded and under which names, that the URL (a credential) never reaches the
output, and that a missing or malformed setting uploads nothing.
"""
from __future__ import annotations

import gzip
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = ROOT / "scripts" / "backup_host.sh"
PAR_TOKEN = "par-token-that-must-stay-private"
FAKE_DOCKER = r'''#!/usr/bin/env bash
echo "$*" >> "$STATE/calls"
service=$4
shift 4
case "$service $1" in
  "daemon cp") [ -e "$STATE/no_register" ] && exit 1; exit 0 ;;
  "daemon uv") cat "$STATE/fingerprint" ;;
  "daemon cat") printf 'register-bytes' ;;
  "daemon rm") exit 0 ;;
  "superset-db pg_dump") printf 'pg-dump-bytes' ;;
  *) echo "unexpected docker call: $service $*" >&2; exit 2 ;;
esac
'''


class _Bucket(BaseHTTPRequestHandler):
    """Accepts PUTs and records them, answering with the server's status."""

    def do_PUT(self) -> None:  # noqa: N802 (the stdlib's handler name)
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.uploads[self.path] = body
        self.send_response(self.server.status)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def bucket():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Bucket)
    server.uploads, server.status = {}, 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def _par_url(bucket) -> str:
    return f"http://127.0.0.1:{bucket.server_port}/p/{PAR_TOKEN}/n/ns/b/nfr-backups/o/"


def _run(tmp_path: Path, env_line: str | None, fingerprint: str = "abc123", has_register: bool = True):
    app, bin_dir, state = tmp_path / "app", tmp_path / "bin", tmp_path / "state"
    for d in (app, bin_dir, state):
        d.mkdir()
    if env_line is not None:
        (app / ".env").write_text(f"SUPERSET_SECRET_KEY=unrelated\n{env_line}\n")
    (state / "fingerprint").write_text(fingerprint)
    if not has_register:
        (state / "no_register").touch()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "NFR_APP_DIR": str(app), "STATE": str(state)}
    result = subprocess.run(["bash", str(BACKUP_SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    calls = (state / "calls").read_text().splitlines() if (state / "calls").exists() else []
    return result, calls


def _output(result) -> str:
    return result.stdout + result.stderr


def test_backup_uploads_the_register_and_superset_records(tmp_path, bucket):
    result, _ = _run(tmp_path, f"NFR_BACKUP_PAR_URL={_par_url(bucket)}")
    assert result.returncode == 0, result.stderr
    names = sorted(path.rsplit("/o/", 1)[1] for path in bucket.uploads)
    assert len(names) == 2, f"expected two uploads, got {names}"
    register, superset = names
    assert register.startswith("register/") and register.endswith("-abc123.duckdb.gz"), (
        "the register is named <stamp>-<fingerprint>.duckdb.gz, like build/published/"
    )
    assert superset.startswith("superset/"), "the database dump goes under superset/"
    assert superset.endswith("-superset.dump")
    uploads = {path.rsplit("/o/", 1)[1]: body for path, body in bucket.uploads.items()}
    assert gzip.decompress(uploads[register]) == b"register-bytes", "the register is uploaded gzipped and whole"
    assert uploads[superset] == b"pg-dump-bytes"


def test_backup_never_prints_the_upload_url(tmp_path, bucket):
    result, _ = _run(tmp_path, f'NFR_BACKUP_PAR_URL="{_par_url(bucket)}"')
    assert result.returncode == 0, result.stderr
    assert PAR_TOKEN not in _output(result), "the PAR URL is a credential and must not be logged"


def test_backup_reads_the_register_from_one_copy(tmp_path, bucket):
    _, calls = _run(tmp_path, f"NFR_BACKUP_PAR_URL={_par_url(bucket)}")
    copy_steps = [c for c in calls if c.startswith("compose exec -T daemon")]
    assert copy_steps[0] == "compose exec -T daemon cp /app/build/nfr.duckdb /tmp/nfr-backup.duckdb"
    assert copy_steps[1].endswith(" /tmp/nfr-backup.duckdb"), "the fingerprint is read from the copy"
    assert copy_steps[2] == "compose exec -T daemon cat /tmp/nfr-backup.duckdb", "the copy is what is uploaded"
    assert calls[-1] == "compose exec -T daemon rm -f /tmp/nfr-backup.duckdb", "the copy is removed afterwards"


def test_backup_is_off_when_the_url_is_not_set(tmp_path, bucket):
    result, calls = _run(tmp_path, "OTHER_SETTING=1")
    assert result.returncode == 0, "an unset URL means backups are off, not a failure"
    assert "backups are off" in result.stdout
    assert calls == [], "nothing is read from the stack"
    assert bucket.uploads == {}


def test_backup_refuses_a_url_that_is_not_a_bucket_par(tmp_path, bucket):
    url = f"http://127.0.0.1:{bucket.server_port}/p/{PAR_TOKEN}/n/ns/b/nfr-backups/o/one-object"
    result, calls = _run(tmp_path, f"NFR_BACKUP_PAR_URL={url}")
    assert result.returncode != 0, "a URL that is not a bucket PAR must be refused"
    assert "must be an Object Storage pre-authenticated request URL" in result.stderr
    assert PAR_TOKEN not in _output(result), "the refusal must not echo the URL"
    assert calls == []
    assert bucket.uploads == {}


def test_backup_reports_a_rejected_upload_without_the_url(tmp_path, bucket):
    bucket.status = 404
    result, _ = _run(tmp_path, f"NFR_BACKUP_PAR_URL={_par_url(bucket)}")
    assert result.returncode != 0, "a rejected upload must fail the backup"
    assert "check that NFR_BACKUP_PAR_URL is current" in result.stderr
    assert PAR_TOKEN not in _output(result)


def test_backup_refuses_a_register_without_a_fingerprint(tmp_path, bucket):
    result, _ = _run(tmp_path, f"NFR_BACKUP_PAR_URL={_par_url(bucket)}", fingerprint="")
    assert result.returncode != 0
    assert "records no publish fingerprint" in result.stderr
    assert bucket.uploads == {}, "nothing is uploaded under a name a restore could not use"


def test_backup_fails_when_no_register_is_published(tmp_path, bucket):
    result, _ = _run(tmp_path, f"NFR_BACKUP_PAR_URL={_par_url(bucket)}", has_register=False)
    assert result.returncode != 0
    assert "no published register" in result.stderr
    assert bucket.uploads == {}


def test_backup_needs_the_env_file(tmp_path, bucket):
    result, _ = _run(tmp_path, None)
    assert result.returncode != 0
    assert ".env is missing" in result.stderr
