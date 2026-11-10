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
    apt-get install -y -q ca-certificates curl git make python3 python3-yaml rsync unattended-upgrades
}

install_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        log "docker and the compose plugin are already installed"
        return
    fi
    log "installing docker engine and the compose plugin from download.docker.com"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$DOCKER_KEYRING"
    chmod a+r "$DOCKER_KEYRING"
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
    log "creating a $SWAP_SIZE swap file at $SWAP_FILE"
    fallocate -l "$SWAP_SIZE" "$SWAP_FILE"
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" >/dev/null
    swapon "$SWAP_FILE"
    grep -q "^$SWAP_FILE " /etc/fstab || printf '%s none swap sw 0 0\n' "$SWAP_FILE" >> /etc/fstab
}

enable_security_upgrades() {
    log "enabling unattended security upgrades"
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
        > /etc/apt/apt.conf.d/20auto-upgrades
}

main() {
    require_root_on_ubuntu
    install_base_packages
    install_docker
    add_user_to_docker_group
    ensure_swap
    enable_security_upgrades
    log "done: $(docker --version); $(docker compose version)"
}

main "$@"
