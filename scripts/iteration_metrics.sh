#!/bin/bash
# Iteration-speed timing shim - wraps one command and records how long it took.
#
# Appends exactly one JSON line per run to the shared metrics store:
#   ~/.local/share/mdt-iteration-metrics/metrics.jsonl
#   {"ts","step","seconds","sha","host","exit"}
#
# The store is machine-local and NOT committed, so the same file is shared by every
# worktree and by the deployed watchdog checkout. scripts/ci_health_check.py reads it back
# for check 5 (iteration-speed regression) and writes CI job durations into it too, which
# is what lets a local `just` timing and a GitHub Actions job be compared on one axis.
#
# Zero LLM tokens: /bin/bash plus one python3 call for the clock and the JSON encoding.
#
# THE ONE SANCTIONED FAIL-OPEN
#   Everything about this repo is deliberately brittle except the metrics WRITE. A missing
#   or unwritable ~/.local/share directory, a full disk, a store owned by another user:
#   none of those may ever be the reason a build fails. Measurement is not the build. So a
#   write failure logs a loud [WARN] to stderr and the wrapped command's own exit code is
#   returned untouched. Nothing else here is fail-open: a missing step name or a missing
#   command is a usage ERROR and exits 64, because a metric filed under the wrong name is
#   worse than no metric.
#
# OVERHEAD
#   Two python3 interpreter starts, measured at 60-80ms total on this Mac. That is under
#   0.4 percent of the ~22s smoke recipe and invisible on the ~82s gate, but it would swamp
#   a sub-second measurement. Do NOT wrap anything that finishes faster than ~1s; record
#   those from inside the tool that already has a running clock.
#
# USAGE
#   scripts/iteration_metrics.sh <step> <command> [args...]
#   scripts/iteration_metrics.sh savepoint-gate python3 scripts/savepoint_gate.py
#
# ENVIRONMENT
#   MDT_ITERATION_METRICS_DIR   override the store directory (tests and CI only)
#
# EXIT CODES
#   64                usage error (no step name, or no command to run)
#   <command's own>   in every other case, including when the metrics write failed
#
# -Claude

set -uo pipefail

#----- configuration ---------------------------------------------------------------

METRICS_DIR="${MDT_ITERATION_METRICS_DIR:-${HOME}/.local/share/mdt-iteration-metrics}"
METRICS_FILE="${METRICS_DIR}/metrics.jsonl"
PYTHON_BIN="/usr/bin/python3"

EXIT_USAGE=64

#----- helpers ---------------------------------------------------------------------

_die_usage() {
    printf '[ERROR] iteration-metrics: %s\n' "$1" >&2
    printf '[ERROR] iteration-metrics: usage: %s <step> <command> [args...]\n' "$0" >&2
    exit "${EXIT_USAGE}"
}

# Short SHA of the tree being measured. A timing with no commit attached cannot be
# compared to anything later, so a failure to read it is announced rather than assumed.
_current_sha() {
    local repo_dir sha
    repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    sha="$(git -C "${repo_dir}" rev-parse --short HEAD 2>/dev/null)"
    if [ -z "${sha}" ]; then
        printf '[WARN] iteration-metrics: no git SHA readable in %s, recording "unknown"\n' \
            "${repo_dir}" >&2
        sha="unknown"
    fi
    printf '%s' "${sha}"
}

# THE fail-open. Never returns nonzero, never aborts the caller.
_append_metric() {
    local step="$1" started="$2" exit_code="$3" sha="$4"
    if ! mkdir -p "${METRICS_DIR}" 2>/dev/null; then
        printf '[WARN] iteration-metrics: cannot create %s, timing for %s NOT recorded\n' \
            "${METRICS_DIR}" "${step}" >&2
        return 0
    fi
    "${PYTHON_BIN}" -c '
import json, sys, time
path, step, started, code, sha, host = sys.argv[1:7]
record = {
    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "step": step,
    "seconds": round(time.time() - float(started), 3),
    "sha": sha,
    "host": host,
    "exit": int(code),
}
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\n")
' "${METRICS_FILE}" "${step}" "${started}" "${exit_code}" "${sha}" "$(hostname -s)" 2>/dev/null \
        || printf '[WARN] iteration-metrics: append to %s failed, timing for %s NOT recorded\n' \
            "${METRICS_FILE}" "${step}" >&2
    return 0
}

#----- main ------------------------------------------------------------------------

_main() {
    [ "$#" -ge 1 ] || _die_usage "no step name given"
    local step="$1"
    shift
    [ "$#" -ge 1 ] || _die_usage "no command given for step '${step}'"

    local sha started exit_code
    sha="$(_current_sha)"
    started="$("${PYTHON_BIN}" -c 'import time; print(time.time())')"

    "$@"
    exit_code=$?

    _append_metric "${step}" "${started}" "${exit_code}" "${sha}"
    return "${exit_code}"
}

_main "$@"
