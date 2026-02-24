#!/usr/bin/env bash
set -euo pipefail

REPO="${RHO_AGENT_REPO:-smith-nathanh/rho-agent}"
INSTALL_DIR="${RHO_AGENT_INSTALL_DIR:-$HOME/.local/bin}"
VERSION="${RHO_AGENT_INSTALL_VERSION:-latest}" # "latest" or a GitHub tag (for example v0.1.0)
METHOD="${RHO_AGENT_INSTALL_METHOD:-auto}"     # auto | binary | uv
USE_SUDO="${RHO_AGENT_USE_SUDO:-0}"

say() {
  printf 'rho-agent installer: %s\n' "$*"
}

warn() {
  printf 'rho-agent installer: %s\n' "$*" >&2
}

die() {
  warn "$*"
  exit 1
}

have() {
  command -v "$1" >/dev/null 2>&1
}

usage() {
  cat <<'EOF'
Install rho-agent and rho-eval.

Usage:
  install.sh [--version <tag>|latest] [--dir <path>] [--method auto|binary|uv]

Environment overrides:
  RHO_AGENT_INSTALL_VERSION   Same as --version
  RHO_AGENT_INSTALL_DIR       Same as --dir (default: ~/.local/bin)
  RHO_AGENT_INSTALL_METHOD    Same as --method (default: auto)
  RHO_AGENT_REPO              GitHub repo (default: smith-nathanh/rho-agent)
  RHO_AGENT_USE_SUDO=1        Use sudo when writing to install dir

Release binary convention:
  GitHub release asset named: rho-agent-<target>.tar.gz
  Tarball should contain executables: rho-agent and rho-eval
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      [[ $# -ge 2 ]] || die "missing value for --version"
      VERSION="$2"
      shift 2
      ;;
    --dir)
      [[ $# -ge 2 ]] || die "missing value for --dir"
      INSTALL_DIR="$2"
      shift 2
      ;;
    --method)
      [[ $# -ge 2 ]] || die "missing value for --method"
      METHOD="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1 (use --help)"
      ;;
  esac
done

case "$METHOD" in
  auto|binary|uv) ;;
  *) die "invalid --method: $METHOD (expected auto|binary|uv)" ;;
esac

download_to() {
  local url="$1"
  local out="$2"
  if have curl; then
    curl --fail --location --silent --show-error "$url" -o "$out"
    return 0
  fi
  if have wget; then
    wget -qO "$out" "$url"
    return 0
  fi
  return 1
}

detect_target() {
  local os arch libc
  os="$(uname -s)"
  arch="$(uname -m)"

  case "$arch" in
    x86_64|amd64) arch="x86_64" ;;
    aarch64|arm64) arch="aarch64" ;;
    *) return 1 ;;
  esac

  case "$os" in
    Darwin)
      printf '%s\n' "${arch}-apple-darwin"
      return 0
      ;;
    Linux)
      libc="gnu"
      if have ldd && ldd --version 2>&1 | grep -qi musl; then
        libc="musl"
      fi
      printf '%s\n' "${arch}-unknown-linux-${libc}"
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

mkdir_install_dir() {
  if [[ "$USE_SUDO" == "1" ]]; then
    sudo mkdir -p "$INSTALL_DIR"
  else
    mkdir -p "$INSTALL_DIR"
  fi
}

install_file() {
  local src="$1"
  local dest="$INSTALL_DIR/$2"
  if [[ "$USE_SUDO" == "1" ]]; then
    sudo install -m 0755 "$src" "$dest"
  else
    install -m 0755 "$src" "$dest"
  fi
}

binary_url_for() {
  local asset="$1"
  if [[ "$VERSION" == "latest" ]]; then
    printf 'https://github.com/%s/releases/latest/download/%s\n' "$REPO" "$asset"
  else
    printf 'https://github.com/%s/releases/download/%s/%s\n' "$REPO" "$VERSION" "$asset"
  fi
}

try_binary_install() {
  local target asset url tmpdir archive bin_agent bin_eval

  target="$(detect_target)" || {
    warn "unsupported platform for binary auto-install (uname: $(uname -s)/$(uname -m))"
    return 1
  }

  asset="rho-agent-${target}.tar.gz"
  url="$(binary_url_for "$asset")"
  say "trying binary install from ${url}"

  if ! have tar; then
    warn "tar is required for binary install"
    return 1
  fi

  tmpdir="$(mktemp -d)"
  archive="$tmpdir/$asset"
  trap 'rm -rf "$tmpdir"' RETURN

  if ! download_to "$url" "$archive"; then
    warn "binary release asset not found or download failed"
    return 1
  fi

  tar -xzf "$archive" -C "$tmpdir"
  bin_agent="$(find "$tmpdir" -type f -name rho-agent | head -n1 || true)"
  bin_eval="$(find "$tmpdir" -type f -name rho-eval | head -n1 || true)"

  [[ -n "$bin_agent" ]] || {
    warn "downloaded archive did not contain executable rho-agent"
    return 1
  }
  [[ -n "$bin_eval" ]] || {
    warn "downloaded archive did not contain executable rho-eval"
    return 1
  }

  mkdir_install_dir
  install_file "$bin_agent" "rho-agent"
  install_file "$bin_eval" "rho-eval"
  say "installed binaries to $INSTALL_DIR"
  return 0
}

ensure_uv() {
  if have uv; then
    return 0
  fi
  say "uv not found; installing uv"
  if ! have sh; then
    die "sh is required to install uv"
  fi
  if have curl; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif have wget; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    die "need curl or wget to install uv"
  fi

  if [[ -x "$HOME/.local/bin/uv" ]]; then
    export PATH="$HOME/.local/bin:$PATH"
  fi
  have uv || die "uv install completed but uv is not on PATH"
}

uv_spec() {
  local base="git+https://github.com/${REPO}.git"
  if [[ "$VERSION" == "latest" ]]; then
    printf '%s\n' "$base"
  else
    printf '%s@%s\n' "$base" "$VERSION"
  fi
}

install_via_uv() {
  local spec
  ensure_uv
  spec="$(uv_spec)"
  say "installing via uv tool from ${spec}"
  uv tool install "$spec"
}

main() {
  if [[ "$METHOD" == "binary" ]]; then
    try_binary_install || die "binary install failed"
  elif [[ "$METHOD" == "uv" ]]; then
    install_via_uv
  else
    if ! try_binary_install; then
      say "falling back to uv tool install"
      install_via_uv
    fi
  fi

  if ! command -v rho-agent >/dev/null 2>&1; then
    warn "rho-agent not found on current PATH yet."
    warn "Add this to your shell profile if needed: export PATH=\"$INSTALL_DIR:\$PATH\""
  fi

  say "done"
}

main "$@"
