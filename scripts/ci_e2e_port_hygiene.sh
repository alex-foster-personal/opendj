#!/usr/bin/env bash
# ci_e2e_port_hygiene.sh -- remove stale Web UI servers before an E2E port claim.
#
# Requirements:
# - ✔︎ Kill every listener in this runner lane's backend and frontend windows.
# - ✔︎ Kill apps.webui.server and Vite processes whose cwd is this workspace.
# - ✔︎ Log the lane, listener port, PID, command, and cwd for every process killed.
#
# Acceptance tests:
# - [if] a stale engine listens in the assigned lane [then ⛔️] the next claim reuses its port.
# - [if] a stale workspace server is killed [then ⛔️] its lane and process identity are missing from the log.
set -euo pipefail

readonly WORKSPACE="$(readlink -f "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}")"
readonly LANE="${MUSIC_DJ_PORT_LANE:?MUSIC_DJ_PORT_LANE is required}"
readonly BACKEND_POOL_START=8680
readonly FRONTEND_POOL_START=9400
readonly POOL_SIZE=120
readonly PORT_LANE_STRIDE=840

[[ "$LANE" =~ ^[0-9]+$ ]] || {
    echo "::error::[e2e-hygiene] MUSIC_DJ_PORT_LANE must be a non-negative integer, got $LANE" >&2
    exit 1
}
command -v lsof >/dev/null || {
    echo "::error::[e2e-hygiene] lsof is required to reclaim stale E2E listeners" >&2
    exit 1
}

declare -A KILLED=()

_describe_process() {
    local pid="$1"
    local cwd command
    cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || echo unknown)"
    command="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || echo unknown)"
    printf 'cwd=%s command=%s' "$cwd" "$command"
}

_kill_stale_process() {
    local pid="$1"
    local reason="$2"
    [[ -n "${KILLED[$pid]:-}" ]] && return
    kill -0 "$pid" 2>/dev/null || return
    echo "[e2e-hygiene] lane=$LANE $reason pid=$pid $(_describe_process "$pid")"
    KILLED["$pid"]=1
    kill -TERM "$pid" 2>/dev/null || true
    kill -KILL "$pid" 2>/dev/null || true
}

_kill_lane_listeners() {
    local backend_start frontend_start line pid port
    backend_start=$((BACKEND_POOL_START + LANE * PORT_LANE_STRIDE))
    frontend_start=$((FRONTEND_POOL_START + LANE * PORT_LANE_STRIDE))
    pid=""
    while IFS= read -r line; do
        if [[ "$line" == p* ]]; then
            pid="${line#p}"
        elif [[ "$line" =~ :([0-9]+)(\ \(LISTEN\))?$ ]]; then
            port="${BASH_REMATCH[1]}"
            if {
                ((port >= backend_start && port < backend_start + POOL_SIZE)) ||
                    ((port >= frontend_start && port < frontend_start + POOL_SIZE))
            } && [[ -n "$pid" ]]; then
                _kill_stale_process "$pid" "port=$port"
            fi
        fi
    done < <(lsof -nP -iTCP -sTCP:LISTEN -Fpn 2>/dev/null || true)
}

_kill_workspace_servers() {
    local process_dir pid cwd command
    for process_dir in /proc/[0-9]*; do
        pid="${process_dir#/proc/}"
        cwd="$(readlink -f "$process_dir/cwd" 2>/dev/null || true)"
        [[ "$cwd" == "$WORKSPACE" ]] || continue
        command="$(tr '\0' ' ' <"$process_dir/cmdline" 2>/dev/null || true)"
        [[ "$command" == *"apps.webui.server"* || "$command" == *"vite"* ]] || continue
        _kill_stale_process "$pid" "workspace-server"
    done
}

echo "[e2e-hygiene] lane=$LANE backend=$((BACKEND_POOL_START + LANE * PORT_LANE_STRIDE))-$((BACKEND_POOL_START + LANE * PORT_LANE_STRIDE + POOL_SIZE - 1)) frontend=$((FRONTEND_POOL_START + LANE * PORT_LANE_STRIDE))-$((FRONTEND_POOL_START + LANE * PORT_LANE_STRIDE + POOL_SIZE - 1)) workspace=$WORKSPACE"
_kill_lane_listeners
_kill_workspace_servers
echo "[e2e-hygiene] lane=$LANE killed=${#KILLED[@]}"
