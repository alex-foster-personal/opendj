#!/usr/bin/env bash
# Reproducible macOS pressure acid test for real Open DJ performance KPIs.
#
# MINI-PRD
#   Run a declared resource squeeze while an operator-provided command captures
#   real deck-load, xrun, and UI-latency metrics before, during, and after it.
#   Persist one self-describing JSON report for cross-machine comparison.
#
#   [if] this runs on Linux [then] it exits before a WKWebView/CoreAudio KPI is misreported
#   [if] a capture omits a required KPI [then] it exits before writing a report
#   [if] a squeeze completes [then] its report contains baseline, during, and after captures

set -euo pipefail
umask 077

readonly EXIT_USAGE=64
readonly EXIT_UNAVAILABLE=69
readonly PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
readonly PRESSURE_WARMUP_SECONDS=1
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

KIND=""
LEVEL=""
DURATION_SECONDS=""
CAPTURE_COMMAND=""
CAPTURE_IMPLEMENTATION=""
CAPTURE_IMPLEMENTATION_PATH=""
CAPTURE_EXECUTABLE_PATH=""
CAPTURE_REPOSITORY=""
CAPTURE_RELATIVE=""
CAPTURE_SHA=""
CAPTURE_BLOB=""
OUTPUT_PATH=""
MACHINE_TAG=""
LIBRARY_SCALE_FIXTURE=""
CONFIRM_REAL_PRESSURE=0
SQUEEZE_PID=""
BOUNDARY_CAPTURE_PID=""
TEMP_DIR=""
HARNESS_SOURCE_SHA=""
HARNESS_SOURCE_DIRTY=""

# ----- output and validation -------------------------------------------------------

_error() {
    printf '[ERROR] perf-squeeze: %s\n' "$1" >&2
}

_usage() {
    cat <<'USAGE'
Usage:
  scripts/perf_squeeze.sh --kind <cpu|memory|disk|memory-notify> --level <value>
      --duration <seconds, at least 2> --capture-command <capture-file> --output <report.json>
      --capture-implementation '<path>@<full-git-sha>'
      [--machine-tag <tag>] [--library-scale-fixture <path>] [--confirm-real-pressure]

The capture file runs four times with PERF_SQUEEZE_PHASE set to baseline,
during, pressure-end, then after. --capture-command must name exactly the same
file as --capture-implementation. It must print exactly one JSON object containing:
  deck_load_ms  finite number >= 0 from a real deck load
  xruns         integer >= 0 from the real session xrun counter
  ui_latency_ms finite number >= 0 from a real UI interaction
  app_build_sha full Git SHA from the measured app's /api/v1/build-info
  app_build_dirty false from the measured app's /api/v1/build-info
  frontend_build_sha full Git SHA from the measured shell or frontend
  frontend_build_dirty false from the measured shell or frontend build stamp
  xrun_session_id stable identifier for the measured xrun counter session

Before pressure starts, the pressure-end capture receives
PERF_SQUEEZE_BOUNDARY_READY=<path> and PERF_SQUEEZE_BOUNDARY_SIGNAL=<path>. It
must attach to the real session, await window.__mdtFlushXruns(), and create the
ready file only after that acknowledged warmup flush, then wait for the signal. When signaled
at squeeze completion, it must await that acknowledgement before reading xruns
and emit xrun_boundary_ack: true.

--capture-implementation records the checked-in capture implementation and its
full Git SHA, for example scripts/perf_capture_webkit.sh@0123456789abcdef0123456789abcdef01234567.

--library-scale-fixture points at a JSON manifest ({"track_count": N}) or an
engine data-dir with state/state.db. The floor is 1000 tracks. The 2-track e2e
bench is not library-scale. When the fixture is absent, the report still writes
library_scale.present=false with track_count null (not 0) and stderr names the
gap. Cross-host comparison uses scripts.perf.squeeze_report compare or
just perf-squeeze-compare; a missing required KPI is rejected, never filled
with zero.

Kinds:
  cpu            stress-ng CPU load, --level is 1..100 percent
  memory         stress-ng VM allocation, --level is a size such as 2G
  disk           stress-ng HDD pressure, --level is a size such as 1G
  memory-notify  macOS notification simulation, --level is warn or critical

cpu, memory, and disk alter real machine resources and require
--confirm-real-pressure. memory-notify is notification-only and cannot stand
in for real starvation. This harness requires macOS because Linux is an
engine-side proxy, not a WKWebView/CoreAudio KPI environment.
USAGE
}

_die_usage() {
    _error "$1"
    _usage >&2
    exit "${EXIT_USAGE}"
}

_is_positive_integer() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

_require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        _error "required command is unavailable: $1"
        exit "${EXIT_UNAVAILABLE}"
    fi
}

_validate_arguments() {
    local capture_path capture_sha capture_dir capture_absolute capture_command_dir capture_command_absolute capture_repo capture_relative expected_blob actual_blob
    case "${KIND}" in
        cpu|memory|disk|memory-notify) ;;
        *) _die_usage "--kind must be cpu, memory, disk, or memory-notify" ;;
    esac
    _is_positive_integer "${DURATION_SECONDS}" && [ "${DURATION_SECONDS}" -ge 2 ] || _die_usage "--duration must be at least 2 seconds"
    [ -n "${CAPTURE_COMMAND}" ] || _die_usage "--capture-command is required"
    [[ "${CAPTURE_IMPLEMENTATION}" =~ ^[^[:space:]@]+@[0-9a-f]{40}$ ]] || _die_usage "--capture-implementation must be <path>@<full-git-sha>"
    capture_path="${CAPTURE_IMPLEMENTATION%@*}"
    capture_sha="${CAPTURE_IMPLEMENTATION##*@}"
    [ -x "${capture_path}" ] || _die_usage "capture implementation must be an executable file: ${capture_path}"
    capture_dir="$(cd "$(dirname "${capture_path}")" && pwd)"
    capture_absolute="${capture_dir}/$(basename "${capture_path}")"
    capture_command_dir="$(cd "$(dirname "${CAPTURE_COMMAND}")" 2>/dev/null && pwd)" || _die_usage "--capture-command must name the verified capture implementation file"
    capture_command_absolute="${capture_command_dir}/$(basename "${CAPTURE_COMMAND}")"
    [ "${capture_command_absolute}" = "${capture_absolute}" ] || _die_usage "--capture-command must name the verified --capture-implementation file"
    capture_repo="$(git -C "${capture_dir}" rev-parse --show-toplevel 2>/dev/null)" || _die_usage "capture implementation must be inside a readable Git checkout: ${capture_path}"
    capture_relative="${capture_absolute#"${capture_repo}/"}"
    [ "${capture_relative}" != "${capture_absolute}" ] || _die_usage "capture implementation must be inside its Git checkout: ${capture_path}"
    git -C "${capture_repo}" ls-files --error-unmatch -- "${capture_relative}" >/dev/null 2>&1 || _die_usage "capture implementation is not tracked by Git: ${capture_path}"
    expected_blob="$(git -C "${capture_repo}" rev-parse "${capture_sha}:${capture_relative}" 2>/dev/null)" || _die_usage "capture implementation is absent from declared Git SHA: ${CAPTURE_IMPLEMENTATION}"
    actual_blob="$(git -C "${capture_repo}" hash-object -- "${capture_relative}")"
    [ "${actual_blob}" = "${expected_blob}" ] || _die_usage "capture implementation does not match declared Git SHA: ${CAPTURE_IMPLEMENTATION}"
    CAPTURE_IMPLEMENTATION_PATH="${capture_absolute}"
    CAPTURE_REPOSITORY="${capture_repo}"
    CAPTURE_RELATIVE="${capture_relative}"
    CAPTURE_SHA="${capture_sha}"
    CAPTURE_BLOB="${expected_blob}"
    [ -n "${OUTPUT_PATH}" ] || _die_usage "--output is required"
    [ -d "$(dirname "${OUTPUT_PATH}")" ] || _die_usage "output parent does not exist: $(dirname "${OUTPUT_PATH}")"
    [ -x "${PYTHON_BIN}" ] || { _error "python interpreter is not executable: ${PYTHON_BIN}"; exit "${EXIT_UNAVAILABLE}"; }
    case "${KIND}" in
        cpu) _is_positive_integer "${LEVEL}" && [ "${LEVEL}" -le 100 ] || _die_usage "cpu --level must be 1 through 100" ;;
        memory|disk) [[ "${LEVEL}" =~ ^[1-9][0-9]*[KMGTP]?$ ]] || _die_usage "${KIND} --level must be a positive size such as 2G" ;;
        memory-notify) [ "${LEVEL}" = warn ] || [ "${LEVEL}" = critical ] || _die_usage "memory-notify --level must be warn or critical" ;;
    esac
    if [ "${KIND}" != memory-notify ] && [ "${CONFIRM_REAL_PRESSURE}" -ne 1 ]; then
        _die_usage "--confirm-real-pressure is required for ${KIND}"
    fi
}

_snapshot_harness_identity() {
    local sha harness_dirty
    sha="${PERF_SQUEEZE_SOURCE_SHA:-$(git rev-parse HEAD)}"
    [[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || {
        _error "PERF_SQUEEZE_SOURCE_SHA must be a full Git SHA, got: ${sha}"
        exit "${EXIT_USAGE}"
    }
    harness_dirty="${PERF_SQUEEZE_HARNESS_DIRTY:-}"
    if [ -z "${harness_dirty}" ]; then
        git status --porcelain >/dev/null 2>&1 || {
            _error "harness source checkout must be clean and readable by git"
            exit "${EXIT_UNAVAILABLE}"
        }
        if [ -n "$(git status --porcelain)" ]; then
            harness_dirty=true
        else
            harness_dirty=false
        fi
    fi
    [ "${harness_dirty}" = false ] || {
        _error "harness source checkout must be clean; PERF_SQUEEZE_HARNESS_DIRTY=${harness_dirty}"
        exit "${EXIT_USAGE}"
    }
    HARNESS_SOURCE_SHA="${sha}"
    HARNESS_SOURCE_DIRTY=false
}

_snapshot_capture_implementation() {
    local snapshot_root snapshot_blob
    snapshot_root="${TEMP_DIR}/capture-checkout"
    mkdir -p "${snapshot_root}"
    if ! git -C "${CAPTURE_REPOSITORY}" archive "${CAPTURE_SHA}" | tar -xf - -C "${snapshot_root}"; then
        _error "could not snapshot the verified capture implementation at ${CAPTURE_IMPLEMENTATION}"
        exit "${EXIT_UNAVAILABLE}"
    fi
    CAPTURE_EXECUTABLE_PATH="${snapshot_root}/${CAPTURE_RELATIVE}"
    [ -x "${CAPTURE_EXECUTABLE_PATH}" ] || {
        _error "verified capture snapshot is not executable: ${CAPTURE_RELATIVE}"
        exit "${EXIT_UNAVAILABLE}"
    }
    snapshot_blob="$(git hash-object -- "${CAPTURE_EXECUTABLE_PATH}")"
    [ "${snapshot_blob}" = "${CAPTURE_BLOB}" ] || {
        _error "verified capture snapshot does not match declared Git SHA: ${CAPTURE_IMPLEMENTATION}"
        exit "${EXIT_UNAVAILABLE}"
    }
}

_require_macos() {
    if [ "$(uname -s)" != Darwin ]; then
        _error "requires macOS for real WKWebView/CoreAudio KPI capture; Linux is an engine-side proxy only"
        exit "${EXIT_UNAVAILABLE}"
    fi
}

# A signal must never look like success: INT/TERM exit with the conventional 128+signal status so
# the EXIT trap below cleans up with that code, even when bash deferred the trap past a child.
_abort() {
    exit "$1"
}

_cleanup() {
    local exit_code="$?"
    if [ -n "${SQUEEZE_PID}" ] && kill -0 "${SQUEEZE_PID}" 2>/dev/null; then
        kill "${SQUEEZE_PID}" 2>/dev/null || true
        wait "${SQUEEZE_PID}" 2>/dev/null || true
    fi
    if [ -n "${BOUNDARY_CAPTURE_PID}" ] && kill -0 "${BOUNDARY_CAPTURE_PID}" 2>/dev/null; then
        kill "${BOUNDARY_CAPTURE_PID}" 2>/dev/null || true
        wait "${BOUNDARY_CAPTURE_PID}" 2>/dev/null || true
    fi
    [ -z "${TEMP_DIR}" ] || rm -rf "${TEMP_DIR}"
    exit "${exit_code}"
}

# ----- capture and pressure --------------------------------------------------------

_squeeze_python() {
    PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}" "${PYTHON_BIN}" -m scripts.perf.squeeze_report "$@"
}

_warn_library_scale_gap() {
    local resolved reason track_count
    if [ -n "${LIBRARY_SCALE_FIXTURE}" ]; then
        resolved="$(_squeeze_python library-scale --fixture "${LIBRARY_SCALE_FIXTURE}")"
    else
        resolved="$(_squeeze_python library-scale)"
    fi
    reason="$(printf '%s' "${resolved}" | "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin)["reason"] or "")')"
    track_count="$(printf '%s' "${resolved}" | "${PYTHON_BIN}" -c 'import json,sys; value=json.load(sys.stdin)["track_count"]; print("" if value is None else value)')"
    if [ -z "${reason}" ]; then
        return 0
    fi
    if [ -n "${track_count}" ]; then
        printf '[WARN] perf-squeeze: %s (track_count=%s)\n' "${reason}" "${track_count}" >&2
    else
        printf '[WARN] perf-squeeze: %s\n' "${reason}" >&2
    fi
}

_validate_capture_metrics() {
    local phase="$1"
    local raw_path="$2"
    _squeeze_python validate --phase "${phase}" --input "${raw_path}" --output "${TEMP_DIR}/${phase}.json"
}

_capture_metrics() {
    local phase="$1"
    local raw_path="${TEMP_DIR}/${phase}.raw.json"
    local xrun_boundary=0
    if [ "${phase}" = baseline ] || [ "${phase}" = pressure-end ]; then
        xrun_boundary=1
    fi
    PERF_SQUEEZE_PHASE="${phase}" PERF_SQUEEZE_MACHINE_TAG="${MACHINE_TAG}" \
        PERF_SQUEEZE_KIND="${KIND}" PERF_SQUEEZE_LEVEL="${LEVEL}" PERF_SQUEEZE_XRUN_BOUNDARY="${xrun_boundary}" \
        "${CAPTURE_EXECUTABLE_PATH}" >"${raw_path}"
    _validate_capture_metrics "${phase}" "${raw_path}"
}

_start_boundary_capture() {
    local ready_path="${TEMP_DIR}/pressure-boundary.ready"
    local signal_path="${TEMP_DIR}/pressure-boundary.signal"
    PERF_SQUEEZE_PHASE=pressure-end PERF_SQUEEZE_MACHINE_TAG="${MACHINE_TAG}" \
        PERF_SQUEEZE_KIND="${KIND}" PERF_SQUEEZE_LEVEL="${LEVEL}" PERF_SQUEEZE_XRUN_BOUNDARY=1 \
        PERF_SQUEEZE_BOUNDARY_READY="${ready_path}" PERF_SQUEEZE_BOUNDARY_SIGNAL="${signal_path}" \
        "${CAPTURE_EXECUTABLE_PATH}" >"${TEMP_DIR}/pressure-end.raw.json" 2>"${TEMP_DIR}/pressure-end.stderr" &
    BOUNDARY_CAPTURE_PID="$!"
}

_wait_for_boundary_ready() {
    local ready_path="${TEMP_DIR}/pressure-boundary.ready"
    local attempt
    for attempt in $(seq 1 50); do
        [ -f "${ready_path}" ] && return 0
        if ! kill -0 "${BOUNDARY_CAPTURE_PID}" 2>/dev/null; then
            _error "pressure-end capture exited before it armed the xrun boundary; stderr follows"
            cat "${TEMP_DIR}/pressure-end.stderr" >&2
            exit 1
        fi
        sleep 0.1
    done
    _error "pressure-end capture did not arm the xrun boundary within 5 seconds"
    exit 1
}

_wait_for_boundary_capture() {
    if ! wait "${BOUNDARY_CAPTURE_PID}"; then
        _error "pressure-end capture failed; stderr follows"
        cat "${TEMP_DIR}/pressure-end.stderr" >&2
        exit 1
    fi
    BOUNDARY_CAPTURE_PID=""
    _validate_capture_metrics pressure-end "${TEMP_DIR}/pressure-end.raw.json"
}

_start_squeeze() {
    case "${KIND}" in
        cpu)
            _require_command stress-ng
            stress-ng --cpu "$(sysctl -n hw.ncpu)" --cpu-load "${LEVEL}" -t "${DURATION_SECONDS}s" >"${TEMP_DIR}/squeeze.stdout" 2>"${TEMP_DIR}/squeeze.stderr" &
            ;;
        memory)
            _require_command stress-ng
            stress-ng --vm 2 --vm-bytes "${LEVEL}" -t "${DURATION_SECONDS}s" >"${TEMP_DIR}/squeeze.stdout" 2>"${TEMP_DIR}/squeeze.stderr" &
            ;;
        disk)
            _require_command stress-ng
            stress-ng --hdd 2 --hdd-bytes "${LEVEL}" -t "${DURATION_SECONDS}s" >"${TEMP_DIR}/squeeze.stdout" 2>"${TEMP_DIR}/squeeze.stderr" &
            ;;
        memory-notify)
            _require_command memory_pressure
            memory_pressure -S -l "${LEVEL}" -s "${DURATION_SECONDS}" >"${TEMP_DIR}/squeeze.stdout" 2>"${TEMP_DIR}/squeeze.stderr" &
            ;;
    esac
    SQUEEZE_PID="$!"
}

_wait_for_squeeze() {
    if ! wait "${SQUEEZE_PID}"; then
        _error "${KIND} squeeze failed; stderr follows"
        cat "${TEMP_DIR}/squeeze.stderr" >&2
        exit 1
    fi
    SQUEEZE_PID=""
    [ -n "${BOUNDARY_CAPTURE_PID}" ] || {
        _error "pressure-end capture was not armed when the squeeze completed"
        exit 1
    }
    : >"${TEMP_DIR}/pressure-boundary.signal"
}

_wait_for_pressure() {
    sleep "${PRESSURE_WARMUP_SECONDS}"
    if ! kill -0 "${SQUEEZE_PID}" 2>/dev/null; then
        _error "${KIND} squeeze exited before the during capture could begin"
        cat "${TEMP_DIR}/squeeze.stderr" >&2
        exit 1
    fi
}

_ensure_pressure_active() {
    if ! kill -0 "${SQUEEZE_PID}" 2>/dev/null; then
        _error "${KIND} squeeze ended during the during capture; report rejected"
        cat "${TEMP_DIR}/squeeze.stderr" >&2
        exit 1
    fi
}

_write_report() {
    local output_tmp write_args
    output_tmp="${TEMP_DIR}/report.json"
    write_args=(
        write
        --baseline "${TEMP_DIR}/baseline.json"
        --during "${TEMP_DIR}/during.json"
        --pressure-end "${TEMP_DIR}/pressure-end.json"
        --after "${TEMP_DIR}/after.json"
        --output "${output_tmp}"
        --machine-tag "${MACHINE_TAG}"
        --hostname "$(hostname -s)"
        --kind "${KIND}"
        --level "${LEVEL}"
        --duration "${DURATION_SECONDS}"
        --harness-source-sha "${HARNESS_SOURCE_SHA}"
        --capture-implementation "${CAPTURE_IMPLEMENTATION}"
    )
    if [ -n "${LIBRARY_SCALE_FIXTURE}" ]; then
        write_args+=(--library-scale-fixture "${LIBRARY_SCALE_FIXTURE}")
    fi
    _squeeze_python "${write_args[@]}"
    mv "${output_tmp}" "${OUTPUT_PATH}"
    printf '[OK] perf-squeeze: report=%s machine=%s kind=%s level=%s duration=%ss\n' "${OUTPUT_PATH}" "${MACHINE_TAG}" "${KIND}" "${LEVEL}" "${DURATION_SECONDS}"
}

# ----- main ------------------------------------------------------------------------

_main() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --kind) KIND="${2:-}"; shift 2 ;;
            --level) LEVEL="${2:-}"; shift 2 ;;
            --duration) DURATION_SECONDS="${2:-}"; shift 2 ;;
            --capture-command) CAPTURE_COMMAND="${2:-}"; shift 2 ;;
            --capture-implementation) CAPTURE_IMPLEMENTATION="${2:-}"; shift 2 ;;
            --output) OUTPUT_PATH="${2:-}"; shift 2 ;;
            --machine-tag) MACHINE_TAG="${2:-}"; shift 2 ;;
            --library-scale-fixture) LIBRARY_SCALE_FIXTURE="${2:-}"; shift 2 ;;
            --confirm-real-pressure) CONFIRM_REAL_PRESSURE=1; shift ;;
            --help|-h) _usage; return 0 ;;
            *) _die_usage "unknown argument: $1" ;;
        esac
    done
    _require_macos
    _validate_arguments
    _warn_library_scale_gap
    _snapshot_harness_identity
    [ -n "${MACHINE_TAG}" ] || MACHINE_TAG="$(hostname -s)-squeeze-${KIND}-${LEVEL}"
    TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mdt-perf-squeeze.XXXXXX")"
    trap _cleanup EXIT
    trap '_abort 130' INT
    trap '_abort 143' TERM
    _snapshot_capture_implementation
    _start_boundary_capture
    _wait_for_boundary_ready
    _capture_metrics baseline
    _start_squeeze
    _wait_for_pressure
    _capture_metrics during
    _ensure_pressure_active
    _wait_for_squeeze
    _wait_for_boundary_capture
    _capture_metrics after
    _write_report
}

_main "$@"
