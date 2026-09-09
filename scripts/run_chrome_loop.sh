#!/usr/bin/env bash
# Start the Chrome dev loop by hand: Python engine on :8728 + vite on :9448.
# Same commands the Claude preview configs run (afmac/.claude/launch.json), so
# the two never drift. Ctrl-C stops both. See docs/architecture/chrome-dev-loop.md.
#
#   scripts/run_chrome_loop.sh            # preflight, engine, vite, tail both logs
#   scripts/run_chrome_loop.sh --engine   # engine only
#   scripts/run_chrome_loop.sh --vite     # vite only
#
# Ports are this worktree's registry pair (python -m apps.webui.port_config claim);
# the data dir is a COPY of the library, so edits made in the loop never reach it.
set -euo pipefail

ENGINE_PORT=8728
VITE_PORT=9448
DATA_DIR="$HOME/Library/Application Support/com.opendj.desktop.chrome-loop"
COMMIT_MAP="$DATA_DIR/rewrite-commit-map-20260903T102735Z"   # only needed while the app predates #911
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS="$DATA_DIR/logs"

want_engine=1; want_vite=1
case "${1:-}" in
  --engine) want_vite=0 ;;
  --vite)   want_engine=0 ;;
  "") ;;
  *) echo "[ERROR] unknown flag: $1 (use --engine, --vite, or nothing)" >&2; exit 2 ;;
esac

_refuse_if_busy() {  # $1 port $2 label
  local pid
  pid="$(lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
  if [[ -n "$pid" ]]; then
    echo "[ERROR] $2 port $1 is already in use by pid $pid ($(ps -o comm= -p "$pid")) - stop it first" >&2
    exit 3
  fi
}

[[ -d "$DATA_DIR" ]] || { echo "[ERROR] loop data dir missing: $DATA_DIR (copy the library there first)" >&2; exit 2; }
mkdir -p "$LOGS"
cd "$ROOT"

(( want_engine )) && _refuse_if_busy "$ENGINE_PORT" engine
(( want_vite ))   && _refuse_if_busy "$VITE_PORT" vite

_kill_tree() {  # $1 pid: children first, so `tee` cannot outlive the server it wraps
  local child
  for child in $(pgrep -P "$1" 2>/dev/null); do _kill_tree "$child"; done
  kill "$1" 2>/dev/null || true
}
_stop_all() {
  echo; echo "[loop] stopping"
  local pid
  for pid in ${pids[@]+"${pids[@]}"}; do _kill_tree "$pid"; done  # bash 3.2 + set -u: empty-array guard
  wait 2>/dev/null || true
}
pids=()
trap _stop_all INT TERM EXIT

if (( want_engine )); then
  map_args=()
  [[ -f "$COMMIT_MAP" ]] && map_args=(--commit-map "$COMMIT_MAP")
  ./.venv/bin/python -m scripts.dev_loop_preflight "${map_args[@]}"
  # Subshell so $! owns BOTH the server and its tee: for a bare `a | b &`, $! is
  # only b, and stopping tee leaves the server listening (seen Thu 3 Sep 2026).
  ( MDT_LIBRARY_MODE=local exec ./.venv/bin/python -m apps.engine_core serve \
      --data-dir "$DATA_DIR" --host 127.0.0.1 --port "$ENGINE_PORT" 2>&1 | tee -a "$LOGS/engine.log" ) &
  pids+=($!)
  echo "[loop] engine -> http://127.0.0.1:$ENGINE_PORT/api/v1/health (log: $LOGS/engine.log)"
fi

if (( want_vite )); then
  ( cd apps/webui/frontend && CI=true env -u MUSIC_DJ_BACKEND_PORT -u MUSIC_DJ_FRONTEND_PORT \
      pnpm exec vite dev --host 127.0.0.1 2>&1 | tee -a "$LOGS/vite.log" ) &
  pids+=($!)
  echo "[loop] vite   -> http://127.0.0.1:$VITE_PORT/performance (log: $LOGS/vite.log)"
fi

wait
