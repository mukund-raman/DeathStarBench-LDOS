#!/usr/bin/env bash

set -euo pipefail # Exit early on errors

# Social Network microservices installer
# - Installs OS deps (Docker, Python3, LuaJIT/LuaRocks, build tools)
# - Installs LuaSocket via luarocks
# - Builds wrk2 load generator
# - Verifies key tools are installed

ROOT_DIR="$(cd "$(dirname "$0")"/.. && pwd)"
SOCIAL_DIR="${ROOT_DIR}/socialNetwork"
WRK2_DIR="${ROOT_DIR}/wrk2"

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

log() {
  echo "[social-net-install] $*"
}

require_root() {
  if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    log "This script needs root privileges for package installation."
    log "Re-running with sudo..."
    exec sudo -E bash "$0" "$@"
  fi
}

install_apt_packages() {
  log "Updating package lists..."
  apt-get update -y

  # Core build tools and libs
  PKGS=(
    build-essential
    git
    curl
    ca-certificates
    pkg-config
    libssl-dev
    zlib1g-dev
    libz-dev
    unzip
    sshpass
  )

  # Python
  PKGS+=(python3 python3-venv python3-pip python3-dev)

  # LuaJIT + LuaRocks
  PKGS+=(luajit libluajit-5.1-dev luarocks)

  # Docker (package names vary by distro version)
  # Prefer docker.io and docker-compose-plugin if available
  if apt-cache show docker.io >/dev/null 2>&1; then
    PKGS+=(docker.io)
  fi
  if apt-cache show docker-compose-plugin >/dev/null 2>&1; then
    PKGS+=(docker-compose-plugin)
  elif apt-cache show docker-compose >/dev/null 2>&1; then
    PKGS+=(docker-compose)
  fi

  log "Installing APT packages: ${PKGS[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${PKGS[@]}"
}

setup_python_venv() {
  local venv_dir="${ROOT_DIR}/.venv"
  log "Setting up Python virtual environment at ${venv_dir}..."
  python3 -m venv "${venv_dir}"
  # shellcheck disable=SC1090
  source "${venv_dir}/bin/activate"
  python -m pip install --upgrade pip setuptools wheel
  if [ -f "${ROOT_DIR}/requirements.txt" ]; then
    log "Installing Python packages from requirements.txt..."
    python -m pip install -r "${ROOT_DIR}/requirements.txt"
  else
    log "requirements.txt not found at ${ROOT_DIR}; skipping Python package installation."
  fi
  deactivate || true
}

install_luarocks_packages() {
  if need_cmd luarocks; then
    log "Installing LuaRocks packages (luasocket)..."
    # Some images need explicit luarocks version arg; try default first.
    luarocks install luasocket || luarocks --lua-version=5.1 install luasocket
  else
    log "luarocks not found; skipping Lua package installs."
  fi
}

enable_docker_service() {
  if need_cmd docker; then
    if need_cmd systemctl; then
      log "Enabling and starting Docker service if available..."
      systemctl enable docker || true
      systemctl start docker || true
    fi
    # Add current user to docker group to run without sudo (effective next login)
    if getent group docker >/dev/null 2>&1; then
      local user_name
      user_name=${SUDO_USER:-${USER}}
      if [ -n "$user_name" ]; then
        log "Adding user '$user_name' to 'docker' group (effective next login)"
        usermod -aG docker "$user_name" || true
      fi
    fi
  fi
}

build_wrk2() {
  if [ ! -d "${WRK2_DIR}" ]; then
    log "wrk2 directory not found at ${WRK2_DIR}"
    return 1
  fi
  log "Building wrk2 in ${WRK2_DIR}..."
  make -C "${WRK2_DIR}" clean >/dev/null 2>&1 || true
  make -C "${WRK2_DIR}"
}

print_next_steps() {
  cat "Done. Next steps:

  1) Start the microservices (from ${SOCIAL_DIR}):
    - docker compose up -d
      or, if using classic docker-compose:
    - docker-compose up -d

  2) Initialize the social graph (from ${SOCIAL_DIR}):
    - source ${ROOT_DIR}/.venv/bin/activate
    - python3 scripts/init_social_graph.py --graph socfb-Reed98
    - deactivate
      (Graphs available under ${SOCIAL_DIR}/datasets/social-graph)

  3) Generate load with wrk2 (from ${SOCIAL_DIR}):
    - ${WRK2_DIR}/wrk -D exp -t 2 -c 64 -d 30s -L \
        -s ./wrk2/scripts/social-network/compose-post.lua \
        http://localhost:8080/wrk2-api/post/compose -R 1000

  4) Frontend:
    - Open http://localhost:8080 in your browser

  5) Traces:
    - Open Jaeger at http://localhost:16686

  If 'docker compose' is unavailable on your system, use 'docker-compose'.
  If Docker requires sudo, either run with sudo or re-login after group change.
  Python packages are installed in a venv at ${ROOT_DIR}/.venv."
}

main() {
  require_root "$@"

  log "Installing prerequisites (APT)..."
  install_apt_packages

  log "Creating Python virtual environment and installing requirements..."
  setup_python_venv

  log "Installing Lua packages..."
  install_luarocks_packages

  log "Ensuring Docker is enabled..."
  enable_docker_service

  log "Building wrk2..."
  build_wrk2

  log "Verifying key tools..."
  for bin in docker ${WRK2_DIR}/wrk luajit luarocks; do
    if [ -x "$bin" ] || need_cmd "$bin"; then
      log "found: $bin"
    else
      log "warning: missing $bin"
    fi
  done

  print_next_steps
}

main "$@"
