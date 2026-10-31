#!/usr/bin/env bash
# Canonical bootstrap for every cloud coding-agent runner on this repo:
# Claude Code cloud environments, OpenAI Codex Cloud, and Cursor Background
# Agents ((private path) "install"). Each product's setup field
# calls THIS script, so the three cannot drift from each other or from CI.
# Where each one is wired: (private path).
#
# Fail-fast and idempotent. It ends by running (private path),
# so a missing dependency fails the SETUP step, not an agent's task later.
#
#   scripts/cloud_agent_setup.sh          install, then verify
#   scripts/cloud_agent_setup.sh --check  verify only, no installs
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

_log() { printf '[cloud-env] %s\n' "$1"; }
# shellcheck source=scripts/lib/rust_override.sh
. "$REPO_ROOT/scripts/lib/rust_override.sh"
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
# libsqlcipher-dev and just mirror the apt step in .github/workflows/ci.yml.
# pkg-config + libasound2-dev: cpal's ALSA backend (apps/audio-engine
# `--features device`). clang + libclang-dev: bindgen, which the
# signalsmith-stretch crate in apps/audio-engine runs at build time.
# libdbus-1-dev: libdbus-sys in the desktop shells.
# gh: the REST smoke test below, whenever the runner sets a GitHub token.
# openssh-client: the image has no ssh, which the dispatch MCP (.mcp.json,
# served from a fleet-af checkout) needs to reach nucbox-wsl and which the
# tailnet join exists to serve.
# xvfb + libnss3 libgbm1 libgtk-3-0t64 libxss1: run the Electron shell
# (apps/desktop/electron) headless under `xvfb-run`.
# pulseaudio + pulseaudio-utils: a sound server and pactl, so the Linux
# system-volume tests run against a real default sink instead of skipping
# (ci/runner-toolset.yml declares the same pair for the e2e hosts).
# ODJ_CLOUD_TAURI=1 adds the WebKitGTK stack the Tauri shell compiles against;
# it is several hundred MB, so it is opt-in.
APT_PACKAGES="ca-certificates curl git libsqlcipher-dev just pkg-config libasound2-dev clang libclang-dev libdbus-1-dev gh openssh-client xvfb libnss3 libgbm1 libgtk-3-0t64 libxss1 pulseaudio pulseaudio-utils"
if [ "${ODJ_CLOUD_TAURI:-0}" = "1" ]; then
  APT_PACKAGES="$APT_PACKAGES libwebkit2gtk-4.1-dev libgtk-3-dev libsoup-3.0-dev libjavascriptcoregtk-4.1-dev librsvg2-dev libayatana-appindicator3-dev"
fi
if [ "$CHECK_ONLY" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
  _log "apt: $APT_PACKAGES"
  # A preinstalled third-party PPA the network policy blocks only warns here;
  # apt-get still exits 0 and the Ubuntu archive is refreshed.
  _as_root apt-get update -qq
  # shellcheck disable=SC2086  # word splitting of the package list is intended
  _as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $APT_PACKAGES >/dev/null
fi

# --- pulseaudio (Linux system-volume tests) -----------------------------------
# A daemon does not survive a snapshot or a reboot, so both modes start one
# when nothing answers: that is runtime state, not an install. `pactl info`
# probes the socket, as scripts/ci_pulse_sink.sh does. A failed start does not
# stop setup; the verify step below turns it red.
if command -v pactl >/dev/null 2>&1 && ! pactl info >/dev/null 2>&1; then
  _log "pulseaudio --start (nothing answered pactl info)"
  pulseaudio --start --exit-idle-time=-1 || _log "pulseaudio --start FAILED; the smoke test will report it"
fi

# --- rust (apps/audio-engine, the native waveform extension) ------------------
# The floor is apps/audio-engine's declared rust-version, which the smoke test
# keeps at or above every rust-version in its locked dependency graph. Images
# ship an older stable (1.83 measured), which cannot even parse that graph's
# edition-2024 crates, so an existing cargo is updated, not only a missing one.
RUST_FLOOR="$(sed -n 's/^rust-version *= *"\([^"]*\)".*/\1/p' apps/audio-engine/Cargo.toml)"
[ -n "$RUST_FLOOR" ] || _die "could not read rust-version from apps/audio-engine/Cargo.toml"
_cargo_version() { cargo --version 2>/dev/null | awk '{print $2}'; }
_cargo_meets_floor() { [ "$(printf '%s\n%s\n' "$RUST_FLOOR" "$(_cargo_version)" | sort -V | head -1)" = "$RUST_FLOOR" ]; }
if ! command -v cargo >/dev/null 2>&1; then
  [ "$CHECK_ONLY" -eq 1 ] && _die "cargo not installed (run without --check first)"
  _log "installing rustup (stable, minimal profile)"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
  export PATH="$HOME/.cargo/bin:$PATH"
fi
command -v cargo >/dev/null 2>&1 || _die "cargo not on PATH after install"
if ! _cargo_meets_floor; then
  [ "$CHECK_ONLY" -eq 1 ] && _die "cargo $(_cargo_version) is older than rust-version $RUST_FLOOR (run without --check first)"
  command -v rustup >/dev/null 2>&1 || _die "cargo $(_cargo_version) is older than rust-version $RUST_FLOOR and there is no rustup to update it"
  _log "cargo $(_cargo_version) is older than rust-version $RUST_FLOOR: rustup update stable"
  rustup update stable --no-self-update
  rustup default stable
  # A directory override or RUSTUP_TOOLCHAIN outranks the default, so the
  # upgrade above cannot reach it. Name it instead of a bare version failure.
  if ! _cargo_meets_floor; then
    _active="$(rustup show active-toolchain 2>/dev/null || true)"
    _hint="$(rust_override_hint "$_active")"
    if [ -n "$_hint" ]; then
      _die "cargo $(_cargo_version) is older than rust-version $RUST_FLOOR because a rustup override pins it ($_active): $_hint, then rerun this script"
    fi
  fi
fi
_cargo_meets_floor || _die "cargo $(_cargo_version) is still older than rust-version $RUST_FLOOR"
_log "cargo $(_cargo_version) meets rust-version $RUST_FLOOR"
# Warm the crate cache so an agent's first `cargo test` compiles, not downloads.
if [ "$CHECK_ONLY" -eq 0 ] && [ -f apps/audio-engine/Cargo.lock ]; then
  _log "cargo fetch --locked (apps/audio-engine)"
  cargo fetch --locked --manifest-path apps/audio-engine/Cargo.toml --quiet
fi

# --- uv + python venv -------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  [ "$CHECK_ONLY" -eq 1 ] && _die "uv not installed (run without --check first)"
  _log "installing uv (astral.sh)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
command -v uv >/dev/null 2>&1 || _die "uv not on PATH after install"
# analysis: librosa, which the generated e2e fixture library
# (apps/webui/frontend/tests/e2e/support/deckload_fixture.py) analyzes with.
if [ "$CHECK_ONLY" -eq 0 ]; then
  _log "uv sync --locked --extra dev --extra analysis"
  uv sync --locked --extra dev --extra analysis
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

# --- electron shell (only on trees that carry it) ----------------------------
# electron's postinstall is the Chromium download. pnpm can skip build scripts
# (allowBuilds/onlyBuiltDependencies differ across pnpm majors), so the runtime
# is fetched explicitly; install.js is a no-op once dist/ matches the version.
ELECTRON_DIR=apps/desktop/electron
if [ "$CHECK_ONLY" -eq 0 ] && [ -f "$ELECTRON_DIR/pnpm-lock.yaml" ]; then
  _log "pnpm install --frozen-lockfile (electron shell)"
  (cd "$ELECTRON_DIR" && pnpm install --frozen-lockfile --reporter=silent)
  # One retry: the first download through a proxy has been seen to truncate.
  (cd "$ELECTRON_DIR/node_modules/electron" && { node install.js || node install.js; })
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

# --- tailnet (only where an auth key is configured) ---------------------------
# Lets `ssh <machine>` reach the fleet from a cloud session. A failed join does
# not stop the other installs; the verify step below turns it red.
# The documented setup keeps TS_AUTHKEY in Doppler, not in the environment, so
# read it from there when only DOPPLER_TOKEN is raw. Exported so the join and
# the smoke test below both see it; the value is never printed.
if [ -z "${TS_AUTHKEY:-}" ] && [ -n "${DOPPLER_TOKEN:-}" ] && command -v doppler >/dev/null 2>&1; then
  # --no-fallback is a `doppler run` flag; `doppler secrets get` rejects it
  # with "unknown flag" and exits 1, which silently emptied the key.
  TS_AUTHKEY="$(doppler run --no-fallback -- printenv TS_AUTHKEY 2>/dev/null || true)"
  if [ -n "$TS_AUTHKEY" ]; then
    export TS_AUTHKEY
    _log "TS_AUTHKEY read from Doppler"
  else
    _log "TS_AUTHKEY could not be read from Doppler (absent from the config, or doppler run failed)"
  fi
fi
if [ -n "${TS_AUTHKEY:-}" ] && [ "$CHECK_ONLY" -eq 0 ]; then
  bash scripts/cloud_tailnet_join.sh || _log "tailnet join FAILED; the smoke test will report it"
elif [ -z "${TS_AUTHKEY:-}" ]; then
  _log "TS_AUTHKEY not set in the environment or Doppler: no tailnet, the tailnet check will SKIP with a stated reason"
fi

# --- fixture pack (opt-in) -----------------------------------------------------
# ODJ_FIXTURE_PREFETCH=1 in the environment unpacks the newest fixture pack
# (fully analyzed real tracks; (private path)) in the background, so setup
# is not held up by a download. Off by default: most sessions never need audio,
# and any session can run `python scripts/fixtures/pack.py ensure` on demand.
if [ "${ODJ_FIXTURE_PREFETCH:-0}" = "1" ] && [ "$CHECK_ONLY" -eq 0 ]; then
  mkdir -p "$HOME/.cache/odj-fixtures"
  nohup .venv/bin/python scripts/fixtures/pack.py ensure >"$HOME/.cache/odj-fixtures/prefetch.log" 2>&1 &
  _log "fixture pack prefetch started in the background (log: ~/.cache/odj-fixtures/prefetch.log)"
fi

# --- verify ------------------------------------------------------------------
_log "running (private path)"
CLOUD_ENV_DOCTOR=1 .venv/bin/python -m pytest (private path) -q -p no:cacheprovider
_log "cloud environment ready"
