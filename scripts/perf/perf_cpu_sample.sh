#!/usr/bin/env bash
#
# perf_cpu_sample.sh -- native CPU sampling of one Open DJ process, by ROLE.
#
# /usr/bin/sample is the workhorse of this harness: it ships in the base OS,
# needs no sudo, and attaches to the packaged (hardened-runtime) app. It is the
# ONLY native CPU profiler available on this machine -- xctrace needs a full
# Xcode that is not installed, dtrace is SIP-blocked, and `perf` does not exist
# on macOS. See docs/perf/profiling-harness.md.
#
# FILE REQUIREMENTS (mini-PRD)
#
# * R1 a role samples the process the diagnostics probe would attribute to it.
#   Status: OK, run, works as expected.
#   [if `webkit-webcontent` matched by name then one of TEN unrelated
#    WebContent processes on this Mac could be sampled instead]
#   [if the app is not running then this exits non-zero and samples nothing]
#   [if a bare pid is passed then it is sampled without any role lookup]
# * R2 a finished sample is PROVEN to contain a call graph before any verdict
#   is printed. Status: OK, run, works as expected.
#   [if sample writes a truncated file then the missing `Call graph:` header
#    aborts with UNKNOWN rather than printing an empty top-symbol list]
#   [if the process exits mid-sample then the run fails loudly]
# * R3 the hottest symbols are read out of sample's own output, never guessed.
#   Status: OK, run, works as expected.
#   [if the process is idle then fewer than ten symbols print, honestly, with
#    the count stated -- sample only collapses stacks seen five or more times]
#
# Usage:
#   scripts/perf/perf_cpu_sample.sh python-engine 5
#   scripts/perf/perf_cpu_sample.sh all 3
#   scripts/perf/perf_cpu_sample.sh 53970 10
#   scripts/perf/perf_cpu_sample.sh python-engine 5 --shell-pid 53970   # two builds running
#
# Output: .tmp/perf/<utc>-cpu/<role>-<pid>.sample.txt   (.tmp/ is gitignored)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SAMPLE_BIN=/usr/bin/sample
TOP_SYMBOLS=10

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
SECONDS_TO_SAMPLE="${POSITIONAL[1]:-3}"

if [[ -z "$TARGET" ]]; then
    echo "[ERROR] usage: $0 <role|pid> [seconds] [--shell-pid PID]" >&2
    echo "[ERROR] roles: desktop-shell python-engine engine-worker webkit-webcontent webkit-gpu webkit-networking all" >&2
    exit 2
fi
if [[ ! -x "$SAMPLE_BIN" ]]; then
    echo "[ERROR] $SAMPLE_BIN is missing. It ships in the base OS; this machine is not one this harness can profile." >&2
    exit 2
fi
if ! [[ "$SECONDS_TO_SAMPLE" =~ ^[0-9]+$ ]] || [[ "$SECONDS_TO_SAMPLE" -lt 1 ]]; then
    echo "[ERROR] seconds must be a whole number >= 1, got '${SECONDS_TO_SAMPLE}'" >&2
    exit 2
fi

# ---------------------------------------------------------- resolve targets
# One line per target, "role<TAB>pid". A bare pid skips role resolution
# entirely so a profiler can be pointed at anything, including a process this
# repo knows nothing about.
if [[ "$TARGET" =~ ^[0-9]+$ ]]; then
    if ! kill -0 "$TARGET" 2>/dev/null; then
        echo "[ERROR] no process with pid ${TARGET} (or it is not yours to signal)" >&2
        exit 3
    fi
    TARGETS="pid-${TARGET}"$'\t'"${TARGET}"
else
    # resolve_role exits non-zero and explains itself when the role is unknown
    # or unfilled; set -e turns that into this script's exit, which is what a
    # negative control must see.
    RESOLVE_ARGS=("$TARGET" --format table)
    [[ -n "$SHELL_PID" ]] && RESOLVE_ARGS+=(--shell-pid "$SHELL_PID")
    TARGETS="$(cd "$REPO_ROOT" && /usr/bin/python3 -m scripts.perf.resolve_role "${RESOLVE_ARGS[@]}")"
fi

mkdir -p "${REPO_ROOT}/.tmp/perf"
# mktemp -d creates exclusively: two runs for the same role/PID in the same
# UTC second cannot select the same directory and race `sample`'s report path
# (Codex P2/NON-BLOCKING, #705, perf_cpu_sample.sh:95).
OUT_DIR="$(mktemp -d "${REPO_ROOT}/.tmp/perf/$(date -u +%Y%m%dT%H%M%SZ)-cpu-XXXXXX")"
echo "[OK] output directory: ${OUT_DIR}"

# ------------------------------------------------------------------ sample
sample_one() {
    local role="$1" pid="$2"
    local out="${OUT_DIR}/${role}-${pid}.sample.txt"
    echo
    echo "=== ${role} (pid ${pid}), ${SECONDS_TO_SAMPLE}s ==="
    # sample exits non-zero if the process dies mid-run; let set -e carry that.
    "$SAMPLE_BIN" "$pid" "$SECONDS_TO_SAMPLE" -f "$out" >/dev/null

    # Prove the PRESENCE of a real measurement before reporting anything from
    # it. An absent call graph means the sample failed, and an empty symbol
    # list from a failed sample is indistinguishable from a genuinely idle
    # process (.claude/rules/verification.md).
    if ! grep -q '^Call graph:' "$out"; then
        echo "[ERROR] UNKNOWN: ${out} has no 'Call graph:' section, so nothing was measured." >&2
        exit 4
    fi
    if ! grep -q '^Sort by top of stack' "$out"; then
        echo "[ERROR] UNKNOWN: ${out} has no top-of-stack section, so no symbol ranking exists." >&2
        exit 4
    fi

    echo "[OK] wrote ${out} ($(wc -l <"$out" | tr -d ' ') lines)"
    echo "--- hottest symbols by SELF time (sample's own 'Sort by top of stack, same collapsed') ---"
    # sample only collapses a stack it saw >= 5 times, so an idle process
    # legitimately yields fewer than TOP_SYMBOLS rows. The count is printed so
    # a short list reads as "idle", never as "the parser broke".
    local ranked
    ranked="$(sed -n '/^Sort by top of stack/,/^$/p' "$out" | sed '1d' | sed '/^[[:space:]]*$/d')"
    if [[ -z "$ranked" ]]; then
        echo "    (none: no stack was seen 5 or more times; the process was idle)"
    else
        # `head` via a pipe closes early once satisfied, and `echo`'s side of
        # that pipe dies of SIGPIPE; with `pipefail` set that fails the whole
        # pipeline and, under `set -e`, the script -- even though the run
        # itself succeeded. A here-string hands `head` the same bytes without
        # a pipe to break.
        head -n "$TOP_SYMBOLS" <<<"$ranked"
        echo "    [${TOP_SYMBOLS} shown at most; $(wc -l <<<"$ranked" | tr -d ' ') collapsed stacks in the file]"
    fi
}

while IFS=$'\t' read -r role pid; do
    [[ -n "${role:-}" && -n "${pid:-}" ]] || continue
    sample_one "$role" "$pid"
done <<<"$TARGETS"

echo
echo "[OK] done. Full reports: ${OUT_DIR}"
