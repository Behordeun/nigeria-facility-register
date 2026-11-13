#!/usr/bin/env bash
# Deploy a release tag on the host that runs the compose stack.
#
# The deploy workflow (.github/workflows/deploy.yml) calls this over SSH with a
# key that authorized_keys pins to this script (a forced command), so the key
# can deploy a release and do nothing else. The tag arrives in
# SSH_ORIGINAL_COMMAND; run by hand, pass it as the argument:
#
#     scripts/deploy_host.sh v0.9.0
#
# It runs in two stages. The first, from the copy already on the host,
# fast-forwards the checkout's main branch to the tag, then hands over to the
# tag's own copy of this script, so a release always deploys with its own steps.
# The second rebuilds the images, restarts the stack, waits for Superset,
# re-imports the dashboard (and the public-viewer role, when that account is
# configured), checks both UIs answer and starts a refresh, so the release's
# own code builds the register.
#
# It refuses, without changing anything, when the tag is not a release tag on
# main, when tracked files on the host have local changes, when the checkout is
# not on main, or when the host is already past the tag (rolling the code back
# is a manual step). Volumes are untouched: the register, the archived
# registers and Superset's records survive. While a register is held after a
# rollback, no refresh is started.
set -euo pipefail

APP_DIR="${NFR_APP_DIR:-$HOME/nigeria-facility-register}"
RELEASE_TAG_PATTERN='^v[0-9]+\.[0-9]+\.[0-9]+$'
HEALTH_TIMEOUT_SECONDS=600
SUPERSET_HEALTH_URL="http://127.0.0.1:8088/health"
DAGSTER_HEALTH_URL="http://127.0.0.1:3000/server_info"
# Inside the daemon container; written by scripts/restore_register.py.
HOLD_FILE=/app/build/publish_hold.json

log() { printf '%s deploy: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { log "$*" >&2; exit 1; }

# The tag comes from the SSH client, so it is checked against the release
# pattern before it reaches git, and echoed back only shortened and printable.
validated_tag() {
    local tag=$1
    [[ "$tag" =~ $RELEASE_TAG_PATTERN ]] && { printf '%s' "$tag"; return; }
    local shown=${tag//[^[:alnum:]._-]/?}
    fail "expected a release tag such as v0.9.0, got '${shown:0:40}'"
}

check_out_release() {
    local tag=$1
    cd "$APP_DIR"
    [ "$(git symbolic-ref --short -q HEAD || true)" = main ] \
        || fail "the checkout in $APP_DIR must be on main"
    if ! { git diff --quiet && git diff --cached --quiet; }; then
        fail "tracked files in $APP_DIR have local changes; commit or discard them first"
    fi
    git fetch --quiet --tags origin main
    git rev-parse -q --verify "refs/tags/$tag^{commit}" >/dev/null \
        || fail "tag $tag is not on origin"
    git merge-base --is-ancestor "$tag" origin/main \
        || fail "$tag is not on main"
    if [ "$(git rev-parse HEAD)" != "$(git rev-parse "$tag^{commit}")" ] \
        && git merge-base --is-ancestor "$tag" HEAD; then
        fail "the host is already past $tag ($(git describe --tags --always HEAD)); roll back by hand"
    fi
    log "moving main from $(git describe --tags --always HEAD) to $tag"
    git merge --ff-only --quiet "$tag"
}

# Waits on the image's own healthcheck. The container is looked up on every
# pass, so a container that compose replaces while starting is still followed.
# An image without a healthcheck reports none; the caller's URL check then
# stands in for it.
wait_until_healthy() {
    local service=$1 container status deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
    while :; do
        container=$(docker compose ps -q "$service")
        [ -n "$container" ] || fail "$service is not running"
        status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container")
        case $status in
            healthy) return ;;
            none) log "$service has no healthcheck; relying on its URL check"; return ;;
            unhealthy) fail "$service reports unhealthy; see docker compose logs $service" ;;
        esac
        ((SECONDS < deadline)) || fail "$service not healthy after ${HEALTH_TIMEOUT_SECONDS}s"
        sleep 5
    done
}

wait_for_url() {
    local url=$1 deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
    until curl -fsS -o /dev/null --max-time 10 "$url"; do
        ((SECONDS < deadline)) || fail "$url did not answer within ${HEALTH_TIMEOUT_SECONDS}s"
        sleep 5
    done
}

# Checks only that the variable is non-empty inside the container; the value
# never leaves it.
public_viewer_configured() {
    docker compose exec -T superset sh -c '[ -n "${SUPERSET_PUBLIC_VIEWER_PASSWORD:-}" ]'
}

apply_release() {
    local tag=$1
    cd "$APP_DIR"
    [ -f .env ] || fail ".env is missing in $APP_DIR; see docs/DEPLOY_ORACLE.md, step 4"
    log "building images for $tag"
    docker compose build
    log "starting the stack"
    docker compose up -d
    wait_until_healthy superset
    wait_for_url "$SUPERSET_HEALTH_URL"
    log "importing the dashboard"
    make dashboard PY=python3
    if public_viewer_configured; then
        log "applying the public-viewer role"
        make public-viewer PY=python3
    fi
    wait_for_url "$DAGSTER_HEALTH_URL"
    launch_refresh
    log "$tag deployed"
}

# The sensor only notices changed inputs or dbt models, so a release that
# changes only the orchestration code would otherwise wait for the weekly
# schedule. A refresh already queued or running needs no check: runs are
# serialised, and rebuilding the same inputs republishes the same figures.
launch_refresh() {
    if docker compose exec -T daemon test -f "$HOLD_FILE"; then
        log "a register is held after a rollback ($HOLD_FILE); not starting a refresh"
        return
    fi
    log "starting a refresh"
    docker compose exec -T daemon uv run dagster job launch \
        -m nfr.orchestration.definitions -j refresh_register
}

main() {
    # Assigned on its own line: set -e does not stop on a failed substitution
    # inside another command's arguments.
    local tag
    if [ "${1:-}" = "--apply" ]; then
        tag=$(validated_tag "${2:-}")
        apply_release "$tag"
        return
    fi
    tag=$(validated_tag "${1:-${SSH_ORIGINAL_COMMAND:-}}")
    check_out_release "$tag"
    exec "$APP_DIR/scripts/deploy_host.sh" --apply "$tag"
}

main "$@"
