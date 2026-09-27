#!/usr/bin/env bash
# Render perf KPI launchd agents for the Air. Stops before launchctl unless --install.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="${MDT_PERF_KPI_STATE_DIR:-$HOME/.local/state/af-perf-kpi}"
INSTALL=0
HOST_LABEL="${MDT_PERF_KPI_MACHINE:-}"
NIGHTLY_ONLY=0

while (($#)); do
  case "$1" in
    --install) INSTALL=1; shift ;;
    --state-dir) STATE_DIR="${2:?}"; shift 2 ;;
    --host-label) HOST_LABEL="${2:?}"; shift 2 ;;
    --nightly-only) NIGHTLY_ONLY=1; shift ;;
    *) echo "[ERROR] unknown argument: $1" >&2; exit 2 ;;
  esac
done

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "[ERROR] $name is required for Air install" >&2
    exit 2
  fi
}

require_env MDT_PERF_KPI_SMALL_STABLE_ID
require_env MDT_PERF_KPI_LARGE_STABLE_ID
require_env MDT_PERF_KPI_STEMMED_STABLE_ID
require_env MDT_PERF_KPI_DATA_DIR

# Supersedes: the nightly template's hardcoded `air` MDT_PERF_KPI_MACHINE and
# perf_kpi_config.py's `or "air"` fallback, both removed; the label now comes
# only from this flag (or the variable) and the nightly path requires it.
# --host-label/MDT_PERF_KPI_MACHINE has no hidden default (claude-review,
# PR #3827, round 3, P1/BLOCKING): a silent "air" fallback would attribute
# a second Mac's perf-kpi runs to Air in the ledger with no error -- the
# same corruption already fixed for the sibling dmg-smoke installer in
# round 2 (see .planning/debt/3827.md).
if [ -z "$HOST_LABEL" ]; then
  echo "[ERROR] --host-label (or MDT_PERF_KPI_MACHINE) is required (e.g. --host-label air, --host-label silver)" >&2
  exit 2
fi

mkdir -p "$STATE_DIR" "$HOME/Library/LaunchAgents"

render() {
  local template="$1" target="$2"
  sed \
    -e "s|{{REPO_ROOT}}|$REPO_ROOT|g" \
    -e "s|{{STATE_DIR}}|$STATE_DIR|g" \
    -e "s|{{DATA_DIR}}|$MDT_PERF_KPI_DATA_DIR|g" \
    -e "s|{{HOME}}|$HOME|g" \
    -e "s|{{SMALL_STABLE_ID}}|$MDT_PERF_KPI_SMALL_STABLE_ID|g" \
    -e "s|{{LARGE_STABLE_ID}}|$MDT_PERF_KPI_LARGE_STABLE_ID|g" \
    -e "s|{{STEMMED_STABLE_ID}}|$MDT_PERF_KPI_STEMMED_STABLE_ID|g" \
    -e "s|{{MACHINE}}|$HOST_LABEL|g" \
    "$template" >"$target"
  plutil -lint "$target" >/dev/null 2>&1 || python3 -c "import plistlib; plistlib.loads(open('$target','rb').read())"
}

render "$REPO_ROOT/ops/perf/com.af.perf-kpi-nightly.plist.template" \
  "$HOME/Library/LaunchAgents/com.af.perf-kpi-nightly.plist"

# --nightly-only: for a host with no live-review-preview service at the health leg's
# target (no com.af.opendj-preview-engine listener, e.g. demon-llama), skip the health
# unit entirely rather than install a job shaped to fail every 10 minutes with an
# invalid restart. Both plist templates already stamp their own AF_SERVICE_ID in
# EnvironmentVariables (process-identity attribution), unchanged by this flag.
labels=(com.af.perf-kpi-nightly)
if [[ "$NIGHTLY_ONLY" -ne 1 ]]; then
  render "$REPO_ROOT/ops/perf/com.af.perf-kpi-health.plist.template" \
    "$HOME/Library/LaunchAgents/com.af.perf-kpi-health.plist"
  labels+=(com.af.perf-kpi-health)
fi

if [[ "$INSTALL" -ne 1 ]]; then
  echo "[OK] rendered perf KPI launchd plists (launchctl not touched)"
  exit 0
fi

uid="$(id -u)"
for label in "${labels[@]}"; do
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$uid" "$HOME/Library/LaunchAgents/$label.plist"
done
if [[ "$NIGHTLY_ONLY" -eq 1 ]]; then
  # A host moving from the plain install to --nightly-only still has the health agent
  # loaded, and its plist in LaunchAgents is reloaded at every login. Unload and remove
  # it, or the unit this flag exists to prevent keeps failing every 10 minutes.
  health_plist="$HOME/Library/LaunchAgents/com.af.perf-kpi-health.plist"
  if launchctl print "gui/$uid/com.af.perf-kpi-health" >/dev/null 2>&1; then
    if ! launchctl bootout "gui/$uid/com.af.perf-kpi-health"; then
      echo "[ERROR] com.af.perf-kpi-health is loaded and would not unload; run" \
        "launchctl bootout gui/$uid/com.af.perf-kpi-health, then re-run" >&2
      exit 1
    fi
  fi
  if [[ -e "$health_plist" ]]; then
    rm -f "$health_plist"
    echo "[OK] removed the existing com.af.perf-kpi-health agent (--nightly-only)"
  fi
fi
echo "[OK] perf KPI launchd agents installed"
