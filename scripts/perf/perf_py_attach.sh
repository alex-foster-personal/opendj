#!/usr/bin/env bash
#
# perf_py_attach.sh -- py-spy against the LIVE Python engine, for Python-level
# frames that /usr/bin/sample cannot give you.
#
# `sample` sees CPython's C frames (`_PyEval_EvalFrameDefault` and friends) and
# so answers "how much time is in the interpreter", never "in which of OUR
# functions". py-spy reads the interpreter's frame objects out of the target's
# memory and answers the second question.
#
# THE SUDO WALL IS REAL AND IS NOT A BUG HERE. macOS requires task_for_pid to
# attach to another process's memory, which py-spy needs and `sample` does not,
# so py-spy needs root even against your OWN process. This script therefore
# NEVER invokes sudo (an agent would hang forever on the password prompt): it
# detects non-root and prints the exact command for a human to paste.
#
# The engine is single-worker BY DESIGN (apps/engine_core/config.py fails fast
# on workers != 1), so there is exactly one pid to attach to and no worker
# selection problem.
#
# FILE REQUIREMENTS (mini-PRD)
#
# * R1 the engine pid comes from the probe's family association, not a bare
#   pgrep. Status: OK, run, works as expected.
#   [if a second Open DJ build is running then --shell-pid disambiguates]
#   [if the app is down then this exits non-zero and attaches to nothing]
# * R2 a missing py-spy is INSTALLED, not worked around.
#   Status: OK, run, works as expected.
#   [if py-spy is absent then `uv tool install py-spy` runs (user-level, no
#    sudo) and the run continues]
#   [if uv is also absent then this exits non-zero saying which to install]
# * R3 running unprivileged NEVER hangs on a password prompt.
#   Status: OK, run, works as expected.
#   [if EUID is not 0 then the exact sudo command prints and the exit is 1]
#   [if the script is run under sudo then the profile is actually captured]
#
# Usage:
#   scripts/perf/perf_py_attach.sh 20                     # record a flamegraph for 20s
#   scripts/perf/perf_py_attach.sh --top                  # live top view
#   scripts/perf/perf_py_attach.sh 20 --shell-pid 53970    # pin the build when two run
#
# Output: .tmp/perf/<utc>-pyspy/engine-<pid>.svg   (.tmp/ is gitignored)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

MODE=record
DURATION=20
SHELL_PID=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --top) MODE=top; shift ;;
        --shell-pid)
            SHELL_PID="${2:-}"
            if [[ -z "$SHELL_PID" ]]; then
                echo "[ERROR] --shell-pid needs a pid argument" >&2
                exit 2
            fi
            shift 2
            ;;
        *)
            if [[ "$1" =~ ^[0-9]+$ ]] && [[ "$1" -ge 1 ]]; then
                DURATION="$1"
                shift
            else
                echo "[ERROR] usage: $0 [seconds|--top] [--shell-pid PID]" >&2
                exit 2
            fi
            ;;
    esac
done

# --------------------------------------------------------- resolve the engine
RESOLVE_ARGS=(python-engine)
[[ -n "$SHELL_PID" ]] && RESOLVE_ARGS+=(--shell-pid "$SHELL_PID")
ENGINE_PID="$(cd "$REPO_ROOT" && /usr/bin/python3 -m scripts.perf.resolve_role "${RESOLVE_ARGS[@]}")"
if [[ "$(echo "$ENGINE_PID" | wc -l | tr -d ' ')" != "1" ]]; then
    echo "[ERROR] expected exactly one python-engine pid, got: $(echo "$ENGINE_PID" | tr '\n' ' ')" >&2
    echo "[ERROR] the engine is single-worker by contract (apps/engine_core/config.py), so more than one means two apps are running. Pass --shell-pid to resolve_role to pick." >&2
    exit 3
fi
echo "[OK] python-engine pid ${ENGINE_PID}"

# ------------------------------------------------------------ ensure py-spy
if ! command -v py-spy >/dev/null 2>&1; then
    if ! command -v uv >/dev/null 2>&1; then
        echo "[ERROR] neither py-spy nor uv is on PATH. Install uv (https://docs.astral.sh/uv/) then re-run; this script installs py-spy itself." >&2
        exit 2
    fi
    echo "[OK] py-spy not found; installing it user-level (no sudo needed for the INSTALL)"
    uv tool install py-spy
    # uv tool installs land in ~/.local/bin, which is not always on PATH yet.
    if ! command -v py-spy >/dev/null 2>&1; then
        export PATH="${HOME}/.local/bin:${PATH}"
    fi
fi
if ! command -v py-spy >/dev/null 2>&1; then
    echo "[ERROR] py-spy is still not on PATH after install. Add ~/.local/bin to PATH." >&2
    exit 2
fi
PY_SPY="$(command -v py-spy)"
echo "[OK] py-spy: ${PY_SPY} ($("$PY_SPY" --version))"

mkdir -p "${REPO_ROOT}/.tmp/perf"
# mktemp -d creates exclusively: two runs for the same role/PID in the same
# UTC second cannot collide on this directory the way a plain date-stamped
# path could (Codex P2/NON-BLOCKING, #705, perf_py_attach.sh:106).
OUT_DIR="$(mktemp -d "${REPO_ROOT}/.tmp/perf/$(date -u +%Y%m%dT%H%M%SZ)-pyspy-XXXXXX")"
OUT_SVG="${OUT_DIR}/engine-${ENGINE_PID}.svg"

# ------------------------------------------------------------- the sudo wall
# Checked BEFORE running anything, so the failure is a printed command rather
# than a hung password prompt an agent cannot answer.
if [[ "${EUID}" -ne 0 ]]; then
    echo
    echo "[ERROR] py-spy needs root to attach on macOS (task_for_pid), even to your own process."
    echo "[ERROR] This script will not call sudo, because a non-interactive caller would hang on the prompt."
    echo
    echo "Run this yourself:"
    if [[ "$MODE" == "top" ]]; then
        echo "    sudo ${PY_SPY} top --pid ${ENGINE_PID}"
    else
        echo "    sudo ${PY_SPY} record -o ${OUT_SVG} --pid ${ENGINE_PID} --duration ${DURATION}"
        echo
        echo "Then: open ${OUT_SVG}"
    fi
    echo
    echo "(For Python frames WITHOUT sudo, profile a launched process instead:"
    echo "  scripts/perf/perf_alloc.sh --python-bench <script>   [memray, allocations]"
    echo "  uv run --no-sync --with pyinstrument python -m pyinstrument <script>   [wall time])"
    exit 1
fi

if [[ "$MODE" == "top" ]]; then
    echo "[OK] py-spy top on pid ${ENGINE_PID} (ctrl-c to stop)"
    exec "$PY_SPY" top --pid "$ENGINE_PID"
fi
echo "[OK] recording ${DURATION}s to ${OUT_SVG}"
"$PY_SPY" record -o "$OUT_SVG" --pid "$ENGINE_PID" --duration "$DURATION"
if [[ ! -s "$OUT_SVG" ]]; then
    echo "[ERROR] UNKNOWN: ${OUT_SVG} is empty, so nothing was profiled." >&2
    exit 4
fi
echo "[OK] wrote ${OUT_SVG} ($(wc -c <"$OUT_SVG" | tr -d ' ') bytes)"
