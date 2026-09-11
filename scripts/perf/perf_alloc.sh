#!/usr/bin/env bash
#
# perf_alloc.sh -- memory triage of a LIVE Open DJ process, plus allocation
# profiling of a Python benchmark this script LAUNCHES.
#
# Two different questions, two different tools, and the split is forced by the
# platform:
#
#   live process   vmmap / footprint / leaks   what is resident RIGHT NOW, by
#                                              region and category. No sudo.
#   launched bench memray                      WHO ALLOCATED IT, with a stack.
#                                              Only possible for a process we
#                                              start ourselves, because macOS
#                                              will not let an allocator tracer
#                                              attach to a running task.
#
# THE `leaks` EXIT CODE IS NOT A SUCCESS SIGNAL. `leaks` exits 1 when it FINDS
# leaks and 0 when it finds none, so `set -e` around it turns a successful
# measurement that found something into an aborted script. Worse, it prints a
# "Process N is not debuggable" warning on stderr for hardened-runtime binaries
# (the packaged app is one) and STILL reports a correct leak count underneath.
# So success here is decided by the PRESENCE of the summary line, never by the
# exit code and never by the absence of a warning.
#
# FILE REQUIREMENTS (mini-PRD)
#
# * R1 a live role reports real vmmap / footprint / leak numbers unprivileged.
#   Status: OK, run, works as expected.
#   [if `leaks` exits 1 having found leaks then that is reported as a result,
#    not as a failure]
#   [if no `N leaks for M total leaked bytes` line is produced then the run
#    reports UNKNOWN rather than "no leaks"]
#   [if the process is gone then vmmap fails loudly and nothing is reported]
# * R2 the app being closed never produces a verdict.
#   Status: OK, run, works as expected.
#   [if no Open DJ shell is running then role resolution exits non-zero first]
# * R3 --python-bench profiles allocations of a script it starts itself.
#   Status: OK, run, works as expected.
#   [if the bench path does not exist then the run exits before memray starts]
#   [if memray writes no capture file then the run reports UNKNOWN]
#
# Usage:
#   scripts/perf/perf_alloc.sh webkit-webcontent
#   scripts/perf/perf_alloc.sh python-engine
#   scripts/perf/perf_alloc.sh 53970
#   scripts/perf/perf_alloc.sh python-engine --shell-pid 53970   # two builds running
#   scripts/perf/perf_alloc.sh --python-bench scripts/bench/four_stem_clips.py
#
# Output: .tmp/perf/<utc>-alloc/   (.tmp/ is gitignored)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
mkdir -p "${REPO_ROOT}/.tmp/perf"
# mktemp -d creates exclusively: two invocations in the same UTC second
# cannot collide on this directory the way a plain date-stamped path could
# (Codex P2/NON-BLOCKING, #705, perf_alloc.sh:54).
OUT_DIR="$(mktemp -d "${REPO_ROOT}/.tmp/perf/$(date -u +%Y%m%dT%H%M%SZ)-alloc-XXXXXX")"

usage() {
    echo "[ERROR] usage: $0 <role|pid> [--shell-pid PID]" >&2
    echo "[ERROR]        $0 --python-bench <script.py> [args...]" >&2
    echo "[ERROR] roles: desktop-shell python-engine engine-worker webkit-webcontent webkit-gpu webkit-networking all" >&2
}

# ------------------------------------------------- mode B: launched Python
if [[ "${1:-}" == "--python-bench" ]]; then
    BENCH="${2:-}"
    if [[ -z "$BENCH" ]]; then
        usage
        exit 2
    fi
    if [[ ! -f "$BENCH" ]]; then
        echo "[ERROR] no such bench script: ${BENCH}" >&2
        exit 2
    fi
    shift 2
    mkdir -p "$OUT_DIR"
    CAPTURE="${OUT_DIR}/$(basename "${BENCH%.py}").memray.bin"
    echo "[OK] memray run -> ${CAPTURE}"
    # `--extra dev` is load bearing: memray is declared in pyproject's `dev`
    # OPTIONAL-DEPENDENCY extra, so without it uv resolves an env that has no
    # memray in it and this fails with a bare "Failed to spawn: memray".
    #
    # `--no-sync` is required of every `uv run` launched from this repo
    # (tests/test_uv_run_no_sync.py, issue #1436): a bare `uv run` re-syncs the
    # project env underneath whatever else is running and has taken the live
    # preview down twice. The trade is that this script INSTALLS NOTHING, so
    # the dev extra has to be there already. That is what the preflight below
    # checks, and why it names the exact sync command rather than running one.
    if ! (cd "$REPO_ROOT" && uv run --no-sync --extra dev python -c 'import memray' 2>/dev/null); then
        echo "[ERROR] memray is not installed in the project env, and --no-sync means" >&2
        echo "[ERROR] this script will not install it. Run: uv sync --extra dev" >&2
        exit 3
    fi
    (cd "$REPO_ROOT" && uv run --no-sync --extra dev memray run --output "$CAPTURE" --force "$BENCH" "$@")
    if [[ ! -s "$CAPTURE" ]]; then
        echo "[ERROR] UNKNOWN: memray produced no capture at ${CAPTURE}; nothing was profiled." >&2
        exit 4
    fi
    echo "[OK] capture ${CAPTURE} ($(wc -c <"$CAPTURE" | tr -d ' ') bytes)"
    echo
    echo "Next:"
    echo "    uv run --no-sync --extra dev memray flamegraph ${CAPTURE}   # HTML flamegraph beside it"
    echo "    uv run --no-sync --extra dev memray summary   ${CAPTURE}   # top allocating locations"
    echo "    uv run --no-sync --extra dev memray tree      ${CAPTURE}   # allocation tree"
    echo
    echo "For WALL time rather than allocations:"
    echo "    uv run --no-sync --extra dev python -m pyinstrument ${BENCH}"
    exit 0
fi

# ----------------------------------------------------- mode A: live process
SHELL_PID=""
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --shell-pid)
            SHELL_PID="${2:-}"
            if [[ -z "$SHELL_PID" ]]; then
                echo "[ERROR] --shell-pid needs a pid argument" >&2
                exit 2
            fi
            shift 2
            ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done

TARGET="${POSITIONAL[0]:-}"
if [[ -z "$TARGET" ]]; then
    usage
    exit 2
fi

if [[ "$TARGET" =~ ^[0-9]+$ ]]; then
    if ! kill -0 "$TARGET" 2>/dev/null; then
        echo "[ERROR] no process with pid ${TARGET} (or it is not yours to signal)" >&2
        exit 3
    fi
    TARGETS="pid-${TARGET}"$'\t'"${TARGET}"
else
    RESOLVE_ARGS=("$TARGET" --format table)
    [[ -n "$SHELL_PID" ]] && RESOLVE_ARGS+=(--shell-pid "$SHELL_PID")
    TARGETS="$(cd "$REPO_ROOT" && /usr/bin/python3 -m scripts.perf.resolve_role "${RESOLVE_ARGS[@]}")"
fi

mkdir -p "$OUT_DIR"
echo "[OK] output directory: ${OUT_DIR}"

report_leaks() {
    # Success is the PRESENCE of the summary line. The exit code cannot say it:
    # 0 means "no leaks", 1 means "leaks found", and both are measurements.
    local pid="$1" out="$2"
    local rc=0
    /usr/bin/leaks "$pid" >"$out" 2>"${out}.stderr" || rc=$?
    local summary
    summary="$(grep -E '^Process [0-9]+: [0-9]+ leaks? for [0-9]+ total leaked bytes' "$out" || true)"
    if [[ -z "$summary" ]]; then
        # UNAVAILABLE, and it must reach the EXIT CODE. Hardened-runtime
        # permissions can stop leaks producing a usable measurement at all,
        # and printing UNKNOWN while returning 0 let the run finish "[OK] done"
        # with exit 0 - so automation recorded a profiling run that measured
        # nothing as a successful one. A tool that could not measure must not
        # render a verdict. Codex found it on #705.
        echo "    UNKNOWN: leaks exited ${rc} and produced no summary line."
        echo "    stderr: $(head -c 300 "${out}.stderr" | tr '\n' ' ')"
        return 5
    fi
    echo "    ${summary}   (leaks exit ${rc}: 0 = none found, 1 = leaks found)"
    if [[ -s "${out}.stderr" ]]; then
        # Expected for the packaged app: hardened runtime blocks dumping the
        # CONTENTS of leaked buffers. The COUNT above is still correct.
        echo "    note: $(head -c 200 "${out}.stderr" | tr '\n' ' ')"
    fi
}

triage_one() {
    local role="$1" pid="$2"
    echo
    echo "=== ${role} (pid ${pid}) ==="

    local vm="${OUT_DIR}/${role}-${pid}.vmmap.txt"
    /usr/bin/vmmap -summary "$pid" >"$vm"
    if ! grep -q 'TOTAL' "$vm"; then
        echo "[ERROR] UNKNOWN: ${vm} has no TOTAL row, so vmmap measured nothing." >&2
        exit 4
    fi
    echo "  vmmap -summary -> ${vm}"
    grep -E 'Physical footprint|TOTAL' "$vm" | sed 's/^/    /'

    local fp="${OUT_DIR}/${role}-${pid}.footprint.txt"
    /usr/bin/footprint -p "$pid" >"$fp"
    echo "  footprint -> ${fp}"
    grep -E 'Footprint:' "$fp" | sed 's/^/    /'
    echo "    top categories:"
    sed -n '/Dirty *Clean/,/^$/p' "$fp" | sed -n '3,7p' | sed 's/^/     /'

    echo "  leaks -> ${OUT_DIR}/${role}-${pid}.leaks.txt"
    # Accumulated rather than fatal: the remaining roles are still worth
    # triaging, and vmmap and footprint DID measure this one. The count is
    # what the exit code is built from below.
    report_leaks "$pid" "${OUT_DIR}/${role}-${pid}.leaks.txt" || \
        LEAKS_UNAVAILABLE=$((LEAKS_UNAVAILABLE + 1))
}

LEAKS_UNAVAILABLE=0
while IFS=$'\t' read -r role pid; do
    [[ -n "${role:-}" && -n "${pid:-}" ]] || continue
    triage_one "$role" "$pid"
done <<<"$TARGETS"

echo
if [[ "$LEAKS_UNAVAILABLE" -gt 0 ]]; then
    echo "[WARN] UNAVAILABLE: ${LEAKS_UNAVAILABLE} role(s) produced no leaks" \
         "summary, so their leak count is unmeasured, not zero." >&2
    echo "[WARN] partial. Full reports: ${OUT_DIR}" >&2
    exit 5
fi
echo "[OK] done. Full reports: ${OUT_DIR}"
