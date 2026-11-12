#!/usr/bin/env bash
# Prepare a fresh Ubuntu host to run the compose stack.
#
# Installs Docker Engine and the compose plugin from Docker's own apt
# repository, plus git, make and python3-yaml (what `make dashboard PY=python3`
# needs, so the host needs no project virtualenv). Adds a swap file when the host
# has none, so image builds on a small machine do not run out of memory, turns on
# unattended security upgrades, and lets the invoking user run docker without
# sudo. Safe to run again: each step checks whether it is already done.
#
# It also installs the nfr-backup systemd timer, which runs
# scripts/backup_host.sh nightly as the invoking user; the script does nothing
# until NFR_BACKUP_PAR_URL is set in .env (docs/DEPLOY_ORACLE.md, "Backups").
#
# Usage (on the host, from the repository root):
#     sudo bash scripts/bootstrap_host.sh
# Then log out and back in so the docker group membership applies.
#
# Supports Ubuntu 22.04 and 24.04 on arm64 (Oracle Ampere A1) and amd64. It
# changes no firewall rules: the compose file publishes the Dagster and Superset
# UIs on 127.0.0.1 only, so they are reached over an SSH tunnel.
set -euo pipefail

readonly SWAP_FILE=/swapfile
readonly SWAP_SIZE=2G
readonly DOCKER_KEYRING=/etc/apt/keyrings/docker.asc
readonly DOCKER_SOURCES=/etc/apt/sources.list.d/docker.list
# Primary fingerprint of Docker's apt signing key ("Docker Release (CE deb)").
# The downloaded key is trusted only when it matches, so a substituted key
# can never sign the packages apt then installs as root.
readonly DOCKER_KEY_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
readonly AUTO_UPGRADES=/etc/apt/apt.conf.d/20auto-upgrades
readonly SYSTEMD_DIR=/etc/systemd/system
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_DIR

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] error: %s\n' "$*" >&2; exit 1; }

require_root_on_ubuntu() {
    [ "$(id -u)" -eq 0 ] || die "run with sudo: sudo bash scripts/bootstrap_host.sh"
    [ -r /etc/os-release ] || die "cannot read /etc/os-release; this script supports Ubuntu only"
    # shellcheck source=/dev/null
    . /etc/os-release
    [ "${ID:-}" = "ubuntu" ] || die "unsupported OS '${ID:-unknown}'; this script supports Ubuntu 22.04 and 24.04"
    case "${VERSION_ID:-}" in
        22.04|24.04) ;;
        *) die "unsupported Ubuntu ${VERSION_ID:-unknown}; use 22.04 or 24.04" ;;
    esac
}

install_base_packages() {
    log "installing base packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q ca-certificates curl git gnupg make python3 python3-yaml rsync unattended-upgrades
}

install_docker_key() {
    local tmp_key fingerprint
    tmp_key="$(mktemp)"
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$tmp_key"
    fingerprint="$(gpg --show-keys --with-colons "$tmp_key" 2>/dev/null | awk -F: '/^fpr:/ {print $10; exit}')"
    if [ "$fingerprint" != "$DOCKER_KEY_FINGERPRINT" ]; then
        rm -f "$tmp_key"
        die "Docker's signing key has fingerprint '${fingerprint:-none}', expected $DOCKER_KEY_FINGERPRINT; not trusting it"
    fi
    install -m 0644 "$tmp_key" "$DOCKER_KEYRING"
    rm -f "$tmp_key"
    log "verified Docker's signing key ($DOCKER_KEY_FINGERPRINT)"
}

install_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        log "docker and the compose plugin are already installed"
        # Started at boot either way: the stack's restart policies only bring
        # it back after a reboot once Docker itself is running.
        systemctl enable --now docker
        return
    fi
    log "installing docker engine and the compose plugin from download.docker.com"
    install -m 0755 -d /etc/apt/keyrings
    install_docker_key
    # shellcheck source=/dev/null
    . /etc/os-release
    printf 'deb [arch=%s signed-by=%s] https://download.docker.com/linux/ubuntu %s stable\n' \
        "$(dpkg --print-architecture)" "$DOCKER_KEYRING" "$VERSION_CODENAME" > "$DOCKER_SOURCES"
    apt-get update -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
}

add_user_to_docker_group() {
    local user="${SUDO_USER:-}"
    if [ -z "$user" ] || [ "$user" = "root" ]; then
        log "no sudo user to add to the docker group; skipping"
        return
    fi
    if id -nG "$user" | tr ' ' '\n' | grep -qx docker; then
        log "$user is already in the docker group"
        return
    fi
    usermod -aG docker "$user"
    log "added $user to the docker group; log out and back in for it to apply"
}

ensure_swap() {
    if [ -n "$(swapon --noheadings --show=NAME 2>/dev/null)" ]; then
        log "swap is already active"
        return
    fi
    if [ -e "$SWAP_FILE" ]; then
        # Left by an earlier run that stopped before swapon; it may never have
        # been formatted, so re-initialise it (safe, since it is not in use).
        log "reusing the inactive swap file at $SWAP_FILE"
    else
        log "creating a $SWAP_SIZE swap file at $SWAP_FILE"
        fallocate -l "$SWAP_SIZE" "$SWAP_FILE"
    fi
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" >/dev/null
    swapon "$SWAP_FILE"
    grep -q "^$SWAP_FILE " /etc/fstab || printf '%s none swap sw 0 0\n' "$SWAP_FILE" >> /etc/fstab
}

enable_security_upgrades() {
    if grep -qs '^APT::Periodic::Update-Package-Lists "1";' "$AUTO_UPGRADES" \
        && grep -qs '^APT::Periodic::Unattended-Upgrade "1";' "$AUTO_UPGRADES"; then
        log "unattended security upgrades are already enabled"
        return
    fi
    log "enabling unattended security upgrades"
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > "$AUTO_UPGRADES"
}

# The service runs as the sudo user, who is in the docker group and owns the
# checkout and its .env. The timer is Persistent, so a backup missed while the
# host was off runs at the next boot.
install_backup_timer() {
    local user="${SUDO_USER:-}"
    if [ -z "$user" ] || [ "$user" = "root" ]; then
        log "no sudo user to run backups as; skipping the backup timer"
        return
    fi
    cat > "$SYSTEMD_DIR/nfr-backup.service" <<EOF
[Unit]
Description=Back up the facility register and Superset records off the host
Wants=network-online.target
After=network-online.target docker.service
# A run that fails (the stack still starting after a reboot, say) is retried
# every 15 minutes, up to four tries in two hours.
StartLimitIntervalSec=2h
StartLimitBurst=4

[Service]
Type=oneshot
User=$user
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/scripts/backup_host.sh
Restart=on-failure
RestartSec=15min
# A stuck run is stopped, so the next night's run can start.
TimeoutStartSec=1h
EOF
    cat > "$SYSTEMD_DIR/nfr-backup.timer" <<'EOF'
[Unit]
Description=Nightly off-host backup of the facility register

[Timer]
OnCalendar=*-*-* 02:30:00 UTC
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now nfr-backup.timer >/dev/null
    log "nightly backup timer installed (runs as $user from $REPO_DIR)"
}

main() {
    require_root_on_ubuntu
    install_base_packages
    install_docker
    add_user_to_docker_group
    ensure_swap
    enable_security_upgrades
    install_backup_timer
    log "done: $(docker --version); $(docker compose version)"
}

main "$@"
