#!/usr/bin/env bash
# Canonical bootstrap for every cloud coding-agent runner on this repo:
# Claude Code cloud environments, OpenAI Codex Cloud, and Cursor Background
# Agents (.cursor/environment.json "install"). Each product's setup field
# calls THIS script, so the three cannot drift from each other or from CI.
# Where each one is wired: docs/cloud-agent-environments.md.
#
# Fail-fast and idempotent. It ends by running tests/test_cloud_agent_env.py,
# so a missing dependency fails the SETUP step, not an agent's task later.
#
#   scripts/cloud_agent_setup.sh          install, then verify
#   scripts/cloud_agent_setup.sh --check  verify only, no installs
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

_log() { printf '[cloud-env] %s\n' "$1"; }
_die() { printf '[cloud-env][ERROR] %s\n' "$1" >&2; exit 1; }

# Cloud setup scripts run as root (Claude Code cloud, Codex Cloud), and some
# images have no sudo at all, so sudo is used only when not already root.
_as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    _die "need root or sudo to run: $*"
  fi
}

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

PNPM_PIN="$(sed -n 's/.*"packageManager": *"pnpm@\([^"]*\)".*/\1/p' apps/webui/frontend/package.json)"
[ -n "$PNPM_PIN" ] || _die "could not read the pnpm pin from apps/webui/frontend/package.json packageManager"
export PATH="$HOME/.local/bin:$PATH"

# --- system packages (Linux) ------------------------------------------------
# libsqlcipher-dev mirrors the apt step in .github/workflows/ci.yml.
if [ "$CHECK_ONLY" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
  _log "apt: ca-certificates curl git libsqlcipher-dev"
  _as_root apt-get update -qq
  _as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    ca-certificates curl git libsqlcipher-dev >/dev/null
fi

# --- uv + python venv -------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  [ "$CHECK_ONLY" -eq 1 ] && _die "uv not installed (run without --check first)"
  _log "installing uv (astral.sh)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
command -v uv >/dev/null 2>&1 || _die "uv not on PATH after install"
if [ "$CHECK_ONLY" -eq 0 ]; then
  _log "uv sync --frozen --extra dev"
  uv sync --frozen --extra dev
fi
[ -x .venv/bin/python ] || _die ".venv/bin/python missing (run without --check first)"

# --- frontend (pnpm pinned by packageManager) -------------------------------
command -v node >/dev/null 2>&1 || _die "node not on PATH; all three cloud images ship Node 20+"
# Empty on failure; the caller compares against the pin, so nothing is masked.
_pnpm_version() { (cd apps/webui/frontend && COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm --version 2>/dev/null) || true; }
# corepack only when pnpm is not already the pin: Node 25+ no longer bundles it.
if [ "$CHECK_ONLY" -eq 0 ] && [ "$(_pnpm_version)" != "$PNPM_PIN" ]; then
  command -v corepack >/dev/null 2>&1 || _die "pnpm is not $PNPM_PIN and corepack is missing to activate it"
  # Shims go to ~/.local/bin, not node's own bin dir, which may not be writable.
  corepack enable --install-directory "$HOME/.local/bin" pnpm
  COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack prepare "pnpm@$PNPM_PIN" --activate >/dev/null
fi
PNPM_ACTUAL="$(_pnpm_version)"
[ "$PNPM_ACTUAL" = "$PNPM_PIN" ] || _die "pnpm is '${PNPM_ACTUAL:-missing}', package.json pins $PNPM_PIN"
if [ "$CHECK_ONLY" -eq 0 ]; then
  _log "pnpm install --frozen-lockfile (frontend)"
  (cd apps/webui/frontend && pnpm install --frozen-lockfile --reporter=silent)
fi

# --- doppler (only where a service token is configured) ----------------------
if [ -n "${DOPPLER_TOKEN:-}" ] && ! command -v doppler >/dev/null 2>&1; then
  [ "$CHECK_ONLY" -eq 1 ] && _die "DOPPLER_TOKEN is set but the doppler CLI is missing"
  _log "installing doppler CLI (cli.doppler.com)"
  curl -Ls --tlsv1.2 --proto "=https" https://cli.doppler.com/install.sh \
    | sh -s -- --no-package-manager --install-path "$HOME/.local/bin"
elif [ -z "${DOPPLER_TOKEN:-}" ]; then
  _log "DOPPLER_TOKEN not set: Doppler checks will SKIP with a stated reason"
fi

# --- verify ------------------------------------------------------------------
_log "running tests/test_cloud_agent_env.py"
CLOUD_ENV_DOCTOR=1 .venv/bin/python -m pytest tests/test_cloud_agent_env.py -q -p no:cacheprovider
_log "cloud environment ready"
