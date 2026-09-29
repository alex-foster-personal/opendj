#!/usr/bin/env bash
# Join a Claude Code cloud session to the tailnet so `ssh <machine>` reaches
# the fleet. Opt-in: scripts/cloud_agent_setup.sh calls this only when
# TS_AUTHKEY is set in the cloud environment. Outbound port 22 is blocked in
# those sessions, so plain SSH cannot work; Tailscale runs in userspace mode and
# SSH is tunneled through `tailscale nc`.
#
# Environment:
#   TS_AUTHKEY          reusable, ephemeral, pre-approved, tagged (tag:cloud-agent) key.
#                       Never echoed. Access is decided by the tailnet ACL.
#   ODJ_TAILNET_HOSTNAME  node name, default cloud-agent-<short host id>
#
# Network: the environment needs Custom access with *.tailscale.com and
# pkgs.tailscale.com allowed (docs/cloud-agent-environments.md).
#
# Idempotent: an already-running, logged-in tailscaled is left alone.
set -euo pipefail

_log() { printf '[cloud-tailnet] %s\n' "$1"; }
_die() { printf '[cloud-tailnet][ERROR] %s\n' "$1" >&2; exit 1; }

[ -n "${TS_AUTHKEY:-}" ] || _die "TS_AUTHKEY is not set; nothing to join"

BIN_DIR="$HOME/.local/bin"
STATE_DIR="$HOME/.local/state/tailscale"
SOCKET="$STATE_DIR/tailscaled.sock"
mkdir -p "$BIN_DIR" "$STATE_DIR" "$HOME/.ssh"
export PATH="$BIN_DIR:$PATH"

_ts() { tailscale --socket="$SOCKET" "$@"; }

# The environment-level setup script (fleet-af cloud/environment-setup.sh)
# may already have joined; a second tailscaled would be a second node.
if command -v tailscale >/dev/null 2>&1 \
  && tailscale --socket=/root/.cloud-env/tailscaled.sock status >/dev/null 2>&1; then
  _log "already joined by the environment setup script; leaving it alone"
  exit 0
fi

if command -v tailscale >/dev/null 2>&1 && _ts status >/dev/null 2>&1; then
  _log "already joined: $(_ts status --self --peers=false | head -1)"
  exit 0
fi

# --- install the static binaries ---------------------------------------------
if ! command -v tailscaled >/dev/null 2>&1; then
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *) _die "unsupported architecture $(uname -m)" ;;
  esac
  version="$(curl -fsS 'https://pkgs.tailscale.com/stable/?mode=json' \
    | sed -n 's/.*"TarballsVersion": *"\([^"]*\)".*/\1/p')"
  [ -n "$version" ] || _die "could not read the Tailscale version from pkgs.tailscale.com (is it allowed by the network policy?)"
  _log "installing tailscale $version ($arch)"
  tmp="$(mktemp -d)"
  curl -fsSL "https://pkgs.tailscale.com/stable/tailscale_${version}_${arch}.tgz" \
    | tar -xz -C "$tmp" --strip-components=1
  install -m 0755 "$tmp/tailscale" "$tmp/tailscaled" "$BIN_DIR/"
  rm -rf "$tmp"
fi

# --- start the daemon (userspace: no TUN, no root routing changes) ----------
if ! pgrep -f "tailscaled .*--socket=$SOCKET" >/dev/null 2>&1; then
  _log "starting tailscaled (userspace networking, in-memory state)"
  setsid nohup tailscaled --tun=userspace-networking --state=mem: \
    --statedir="$STATE_DIR" --socket="$SOCKET" \
    >"$STATE_DIR/tailscaled.log" 2>&1 </dev/null &
  for _ in $(seq 1 30); do
    [ -S "$SOCKET" ] && break
    sleep 0.5
  done
  [ -S "$SOCKET" ] || _die "tailscaled did not start; see $STATE_DIR/tailscaled.log"
fi

hostname="${ODJ_TAILNET_HOSTNAME:-cloud-agent-$(hostname | cut -c1-8)}"
_log "joining the tailnet as $hostname"
# The key goes in via a file descriptor, never onto argv or the log.
_ts up --auth-key="file:/dev/stdin" --hostname="$hostname" --accept-dns=false \
  <<<"$TS_AUTHKEY" >/dev/null

# --- ssh through the tailnet --------------------------------------------------
# Userspace mode has no route to 100.x, so ssh tunnels through `tailscale nc`,
# which also resolves MagicDNS names. GitHub keeps its default route.
SSH_CONFIG="$HOME/.ssh/config"
MARKER="# cloud-tailnet (scripts/cloud_tailnet_join.sh)"
if ! grep -qF "$MARKER" "$SSH_CONFIG" 2>/dev/null; then
  _log "adding a tailnet ProxyCommand to $SSH_CONFIG"
  cat >>"$SSH_CONFIG" <<EOF

$MARKER
Host * !github.com !*.github.com
  ProxyCommand $BIN_DIR/tailscale --socket=$SOCKET nc %h %p
  StrictHostKeyChecking accept-new
EOF
  chmod 600 "$SSH_CONFIG"
fi

_log "joined: $(_ts status --self --peers=false | head -1)"
