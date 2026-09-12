#!/usr/bin/env bash
# Render AGT launchd agents for Mac persona scheduling. Stops before launchctl
# unless --install.
#
# USAGE
#   scripts/install_agt_launchd.sh
#   scripts/install_agt_launchd.sh --render-to DIR
#   scripts/install_agt_launchd.sh --install
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${AF_AGT_STATE_DIR:-$HOME/.local/state/af-agt}"
INSTALL=0
RENDER_ONLY=""

while (($#)); do
  case "$1" in
    --install) INSTALL=1; shift ;;
    --render-to)
      RENDER_ONLY="${2:-}"
      [ -n "$RENDER_ONLY" ] || { echo "[ERROR] --render-to needs a path" >&2; exit 2; }
      shift 2
      ;;
    *)
      echo "[ERROR] unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ "$INSTALL" = 1 ] && [ "$(uname -s)" != "Darwin" ]; then
  echo "[ERROR] --install is Darwin-only (this host is $(uname -s))" >&2
  exit 2
fi

FORBIDDEN_MARKERS=(
  "/Applications/Open DJ.app"
  "Library/Application Support/com.opendj.desktop"
  "Library/Application Support/Open DJ"
)

resolve_path() {
  python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

resolved_home="$(resolve_path "$HOME")"
resolved_state="$(resolve_path "$STATE_DIR")"
for marker in "${FORBIDDEN_MARKERS[@]}"; do
  if [[ "$resolved_home" == *"$marker"* ]] || [[ "$resolved_state" == *"$marker"* ]]; then
    echo "[ERROR] forbidden path marker: $marker" >&2
    exit 2
  fi
done

seed_state="$STATE_DIR/seed/state.db"
seed_master="$STATE_DIR/seed/master.plain.db"
if [ ! -f "$seed_state" ] || [ ! -f "$seed_master" ]; then
  echo "[ERROR] missing seed files under $STATE_DIR/seed/" >&2
  echo "        need: $seed_state" >&2
  echo "        need: $seed_master" >&2
  exit 2
fi

xml_escape() {
  local s=$1
  s=${s//&/&amp;}
  s=${s//</&lt;}
  s=${s//>/&gt;}
  printf '%s' "$s"
}

render_plist() {
  local template="$1" target="$2"
  local rendered
  rendered="$(cat "$template")"
  rendered=${rendered//__ABS_HOME__/$(xml_escape "$HOME")}
  rendered=${rendered//__REPO_ROOT__/$(xml_escape "$REPO_ROOT")}
  mkdir -p "$(dirname "$target")"
  printf '%s\n' "$rendered" >"$target"
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$target" >/dev/null
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import plistlib, pathlib, sys; plistlib.loads(pathlib.Path(sys.argv[1]).read_bytes())' "$target"
  fi
}

if [ -n "$RENDER_ONLY" ]; then
  mkdir -p "$RENDER_ONLY"
  render_plist "$REPO_ROOT/ops/agentic-testing/com.af.agt-personas.plist.template" \
    "$RENDER_ONLY/com.af.agt-personas.plist"
  render_plist "$REPO_ROOT/ops/agentic-testing/com.af.agt-soak.plist.template" \
    "$RENDER_ONLY/com.af.agt-soak.plist"
  echo "[OK] rendered AGT plists to $RENDER_ONLY (launchctl not touched)"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$STATE_DIR"
render_plist "$REPO_ROOT/ops/agentic-testing/com.af.agt-personas.plist.template" \
  "$HOME/Library/LaunchAgents/com.af.agt-personas.plist"
render_plist "$REPO_ROOT/ops/agentic-testing/com.af.agt-soak.plist.template" \
  "$HOME/Library/LaunchAgents/com.af.agt-soak.plist"

if [ "$INSTALL" = 1 ]; then
  uid="$(id -u)"
  for label in com.af.agt-personas com.af.agt-soak; do
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$uid" "$HOME/Library/LaunchAgents/$label.plist"
  done
  echo "[OK] AGT launchd agents installed"
  exit 0
fi

echo "[OK] rendered AGT launchd plists (launchctl not touched; use --install after operator approval)"
