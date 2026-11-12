#!/usr/bin/env bash
# Back up the published register and Superset's records off the host.
#
# Uploads two files to an OCI Object Storage bucket:
#
#     register/<UTC stamp>-<fingerprint>.duckdb.gz   the live register, gzipped
#     superset/<UTC stamp>-superset.dump             pg_dump of superset-db
#
# The register's name matches the archive naming in build/published/, so a
# restore is: download, gunzip, copy into build/published/ and run
# `restore_register.py --restore <name>` (docs/DEPLOY_ORACLE.md, "Backups").
#
# The upload goes to a write-only pre-authenticated request (PAR) for the
# bucket, read from NFR_BACKUP_PAR_URL in .env. The host needs no OCI login and
# can only add objects to that one bucket. The URL is a credential, so it is
# passed to curl on stdin and never appears in arguments, logs or errors.
# Retention is the bucket's lifecycle rule, not this script.
#
# Runs nightly from the nfr-backup systemd timer that bootstrap_host.sh
# installs; run it by hand from the repository root to back up now:
#
#     scripts/backup_host.sh
#
# With NFR_BACKUP_PAR_URL unset it does nothing and exits 0: backups are opt-in.
set -euo pipefail

APP_DIR="${NFR_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
URL_KEY=NFR_BACKUP_PAR_URL
LIVE_REGISTER=/app/build/nfr.duckdb
UPLOAD_TIMEOUT_SECONDS=900
FINGERPRINT_PROGRAM='import sys; from nfr.orchestration import publish; print(publish.read_published_fingerprint(sys.argv[1]) or "")'

log() { printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { log "$*" >&2; exit 1; }

# Reads only this one key from .env, so no other setting enters the script. A
# value wrapped in one matching pair of double or single quotes is unwrapped.
configured_url() {
    local line
    line=$(grep -m1 "^${URL_KEY}=" .env || true)
    line=${line#*=}
    if [[ "$line" =~ ^\"(.*)\"$ ]] || [[ "$line" =~ ^\'(.*)\'$ ]]; then
        line=${BASH_REMATCH[1]}
    fi
    printf '%s' "$line"
}

# A bucket PAR ends in /o/, and object names are appended to it. A local
# http://127.0.0.1 address is accepted for tests and drills.
is_bucket_par_url() {
    local url=$1
    [[ "$url" =~ ^https://objectstorage\.[a-z0-9-]+\.oraclecloud\.com/p/[^[:space:]\"]+/o/$ ]] \
        || [[ "$url" =~ ^http://127\.0\.0\.1:[0-9]+/[^[:space:]\"]*/o/$ ]]
}

upload() {
    local file=$1 object=$2
    # curl retries timeouts and 5xx answers on its own; a 4xx (an expired or
    # revoked PAR) fails at once, without the URL in the message.
    printf 'url = "%s%s"\n' "$BACKUP_URL" "$object" \
        | curl --config - --fail --silent --show-error --upload-file "$file" \
            --output /dev/null --max-time "$UPLOAD_TIMEOUT_SECONDS" --retry 3 \
        || fail "upload of $object failed; check that $URL_KEY is current (a PAR expires)"
    log "uploaded $object ($(wc -c < "$file" | tr -d ' ') bytes)"
}

# Writes the gzipped register to the work folder and prints its object name.
copy_out_register() {
    local stamp=$1 fingerprint
    # Copy first, then read and stream the copy: a publish during the backup
    # swaps the live file by rename, so the copy stays one consistent register
    # and its fingerprint matches its contents. The copy's name is unique to
    # this run, so two runs at once never read each other's copy.
    docker compose exec -T daemon cp "$LIVE_REGISTER" "$CONTAINER_COPY" \
        || fail "no published register to back up at $LIVE_REGISTER"
    fingerprint=$(docker compose exec -T daemon uv run python -c "$FINGERPRINT_PROGRAM" "$CONTAINER_COPY")
    [[ "$fingerprint" =~ ^[A-Za-z0-9._-]+$ ]] \
        || fail "the live register records no publish fingerprint; not backing it up"
    docker compose exec -T daemon cat "$CONTAINER_COPY" | gzip -c > "$WORK_DIR/register.duckdb.gz" \
        || fail "copying the register out of the daemon container failed"
    printf 'register/%s-%s.duckdb.gz' "$stamp" "$fingerprint"
}

dump_superset_records() {
    # Custom format is compressed and restores with pg_restore. A bounded lock
    # wait makes a dump blocked by a long-held lock fail, not hang.
    docker compose exec -T superset-db pg_dump -U superset -d superset --format=custom \
        --lock-wait-timeout=5min > "$WORK_DIR/superset.dump" || fail "pg_dump of superset-db failed"
}

# One backup at a time: two runs in the same second would share object names.
# flock is part of util-linux on the Linux host; a machine without it (macOS,
# where only the tests run) skips the lock.
hold_backup_lock() {
    command -v flock >/dev/null 2>&1 || return 0
    exec 9<"${BASH_SOURCE[0]}"
    flock -n 9 || fail "another backup is already running"
}

cleanup() {
    rm -rf "$WORK_DIR"
    if [ -n "${CONTAINER_COPY:-}" ]; then
        docker compose exec -T daemon rm -f "$CONTAINER_COPY" >/dev/null 2>&1 || true
    fi
}

main() {
    cd "$APP_DIR"
    [ -f .env ] || fail ".env is missing in $APP_DIR"
    BACKUP_URL=$(configured_url)
    if [ -z "$BACKUP_URL" ]; then
        log "$URL_KEY is not set in .env; backups are off"
        return
    fi
    is_bucket_par_url "$BACKUP_URL" \
        || fail "$URL_KEY must be an Object Storage pre-authenticated request URL for a bucket, ending in /o/"
    hold_backup_lock
    WORK_DIR=$(mktemp -d)
    trap cleanup EXIT
    local stamp register_object
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    CONTAINER_COPY="/tmp/nfr-backup-${stamp}-$$.duckdb"
    # Both files are made before either is uploaded, so a failed dump leaves no
    # register in the bucket without its Superset records from the same run.
    register_object=$(copy_out_register "$stamp")
    dump_superset_records
    upload "$WORK_DIR/register.duckdb.gz" "$register_object"
    upload "$WORK_DIR/superset.dump" "superset/${stamp}-superset.dump"
    log "backup $stamp complete"
}

main "$@"
