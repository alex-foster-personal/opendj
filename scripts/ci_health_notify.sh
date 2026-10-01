#!/bin/bash
# CI health watchdog notifier - runs scripts/ci_health_check.py and alerts on a nonzero exit.
#
# Invoked by the machine-local launchd agent every 4 hours:
#   ~/Library/LaunchAgents/com.YOU.mdt-ci-health.plist
# Log:
#   ~/Library/Logs/mdt-ci-health.log
# Install one-liner: see the `ci-health` recipe comment in the repo justfile.
#
# Self-update. The WorkingDirectory this runs from is a static clone launchd never
# refreshes on its own, so the first thing _main does is `git pull --ff-only origin
# main` in that directory. A pull that is not a clean fast-forward logs a LOUD [ERROR]
# but never blocks the run - the check still executes on whatever code is already on
# disk, because stale-but-running beats a watchdog that goes dark. See _self_update
# below.
#
# Alert channels. ALL configured channels fire on every alert and each one prints its own
# verdict. A channel that cannot run is reported on every single run, never skipped
# silently, because a watchdog whose alert path has quietly died is worse than no watchdog
# at all. The channels are NOT equal, and the exit code follows the primary one only:
#
#   PRIMARY, must deliver
#     1. email via `gws gmail +send`               configured primary channel
#
#   SECONDARY, best effort, never gate the exit code
#     2. macOS notification via osascript          local desktop channel
#     3. Pushcut push via Doppler PUSHCUT_API_KEY  easy to swipe away unread
#
# Email is the primary delivery channel. A primary-channel failure exits nonzero;
# secondary failures are reported and do not determine delivery success.
#
# Exits nonzero when the health check failed, or when an alert was needed and the PRIMARY
# channel did not deliver it.
#
# Required configuration:
#   MDT_ALERT_EMAIL   the mailbox the PRIMARY channel delivers to. Deployment
#                     specific, so it lives in the environment (or the launchd
#                     plist's EnvironmentVariables), never in this tracked file.
#                     Unset exits nonzero before the check runs: a watchdog with
#                     no recipient is dark, and silence is not a passing verdict.
# Optional configuration:
#   UV_BIN            the uv executable; defaults to $HOME/.local/bin/uv.
#
# Usage:
#   scripts/ci_health_notify.sh          run the check, alert only if it fails
#   scripts/ci_health_notify.sh --test   force one labelled test alert down every channel
#
# -Claude

set -uo pipefail

#----- configuration ---------------------------------------------------------------

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="${REPO_DIR}/scripts/ci_health_check.py"

# uv is installed per user, so its path is derived from HOME rather than spelled
# with a login (#1540). A hard-coded /Users/<name>/.local/bin/uv bound this
# watchdog to one machine and told every reader of the public tree whose it was.
# UV_BIN still wins when set, for a box that installs uv elsewhere.
# An empty HOME makes this a wrong path rather than a guess, and _run_health_check
# already reports a non-executable UV_BIN as [ERROR].
UV_BIN="${UV_BIN:-${HOME}/.local/bin/uv}"
PYTHON_BIN="/usr/bin/python3"
OSASCRIPT_BIN="/usr/bin/osascript"
CURL_BIN="/usr/bin/curl"
DOPPLER_BIN="/opt/homebrew/bin/doppler"

# Resolved by absolute path first, same reasoning as GWS_BIN below: the self-update step
# that reads GIT_BIN runs before anything else in _main, so it should not depend on
# whatever PATH launchd happens to hand us that day.
GIT_BIN_DEFAULT="/usr/bin/git"
if [ -x "${GIT_BIN_DEFAULT}" ]; then
    GIT_BIN="${GIT_BIN_DEFAULT}"
elif command -v git >/dev/null 2>&1; then
    GIT_BIN="$(command -v git)"
else
    GIT_BIN=""
fi

# Gmail CLI used by the configured primary alert channel.
# Install and authenticate it through the deployment operator workflow.
#
# Resolved by absolute path FIRST, matching every other binary above. The previous bare
# `command -v gws` made the primary channel depend on whatever PATH launchd happened to
# hand us. The PATH lookup supports other installation paths; a missing binary
# is an announced degradation, never a no-op.
GWS_BIN_DEFAULT="/opt/homebrew/bin/gws"
if [ -x "${GWS_BIN_DEFAULT}" ]; then
    GWS_BIN="${GWS_BIN_DEFAULT}"
elif command -v gws >/dev/null 2>&1; then
    GWS_BIN="$(command -v gws)"
else
    GWS_BIN=""
fi

# gws derives its macOS keychain account name from USER. Export the current
# user before invoking it so a bare launchd environment preserves that lookup.
: "${USER:=$(id -un)}"
export USER

# The mailbox the PRIMARY channel delivers to. Deployment-specific, so it is
# CONFIGURED, never spelled in the tracked tree (#1540): the address this repo used
# to carry named one person, and a public repository is not the place for it.
#
# Unset does NOT fall back to a default address. A watchdog that mails somewhere
# nobody reads has not alerted, so an unset mailbox fails the precondition below on
# every run and exits nonzero, which is exactly what this script already does for a
# channel that cannot run (see the alert-channels comment at the top).
ALERT_EMAIL="${MDT_ALERT_EMAIL:-}"
# Supersedes: fixed operator deployment defaults; each value is explicit deployment config.
DOPPLER_PROJECT="${MDT_ALERT_DOPPLER_PROJECT:-}"
DOPPLER_CONFIG="${MDT_ALERT_DOPPLER_CONFIG:-}"
PUSHCUT_NOTIFICATION="${MDT_ALERT_PUSHCUT_NOTIFICATION:-}"
LOG_FILE="${HOME}/Library/Logs/mdt-ci-health.log"
HTTP_TIMEOUT_SECONDS=20
# A single GitHub job endpoint can be slow, and a watchdog that waits forever cannot
# report the CI state it was scheduled to observe. The metrics queue is deliberately
# small and resumable, but this is the hard outer boundary for a stuck interpreter,
# resolver, or subprocess. The helper below terminates the complete process group.
HEALTH_CHECK_TIMEOUT_SECONDS=120
GWS_TIMEOUT_SECONDS=30

#----- logging ---------------------------------------------------------------------

_log() {
    local line
    line="$(printf '%s %s' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1")"
    if [ -t 1 ]; then
        # Interactive run: stdout is a terminal, so append to the log file ourselves.
        printf '%s\n' "${line}" | tee -a "${LOG_FILE}"
    else
        # Under launchd, StandardOutPath already points at LOG_FILE. Teeing here as well
        # wrote every line to it twice, which is how this branch came to exist.
        printf '%s\n' "${line}"
    fi
}

# Run one command in its own process group and terminate the whole group on timeout.
# macOS has no POSIX `timeout` utility, and killing only uv can leave its Python or gh
# child behind. On a checker timeout stdout remains a valid JSON receipt so the caller
# can still classify, log, and notify the failed poll rather than hanging on an empty
# command substitution. Set BOUNDED_COMMAND_TIMEOUT_JSON=1 for that form.
_run_command_bounded() {
    local timeout_seconds="$1"
    shift
    "${PYTHON_BIN}" - "${timeout_seconds}" "$@" <<'PY'
import json
import os
import signal
import subprocess
import sys

timeout_seconds = float(sys.argv[1])
command = sys.argv[2:]
try:
    child = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
except OSError as exc:
    if os.environ.get("BOUNDED_COMMAND_TIMEOUT_JSON") == "1":
        print(json.dumps({"error": f"could not start health check: {exc}"}))
    else:
        print(f"could not start command: {exc}", file=sys.stderr)
    raise SystemExit(125)

try:
    output, _ = child.communicate(timeout=timeout_seconds)
except subprocess.TimeoutExpired:
    os.killpg(child.pid, signal.SIGTERM)
    try:
        output, _ = child.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        output, _ = child.communicate()
    if os.environ.get("BOUNDED_COMMAND_TIMEOUT_JSON") == "1":
        print(json.dumps({
            "error": f"health check timed out after {timeout_seconds:.0f}s; process group terminated",
            "timeout_seconds": timeout_seconds,
        }))
    else:
        print(f"command timed out after {timeout_seconds:.0f}s; process group terminated", file=sys.stderr)
    raise SystemExit(124)

sys.stdout.write(output)
raise SystemExit(child.returncode)
PY
}

#----- self-update -------------------------------------------------------------------

# A static launchd checkout needs an explicit refresh to observe current source.
#
# Pulling first, every run, closes that gap automatically. A pull failure is LOUD - an
# [ERROR] line into this same log, plus a nonzero return from this function - but it is
# never fatal to the run: the check still executes afterward on whatever code is already
# on disk. Stale code that keeps running beats a watchdog that goes dark because origin
# force-pushed, a merge conflict landed, or someone left local changes in the checkout.
_self_update() {
    if [ -z "${GIT_BIN}" ]; then
        _log "[ERROR] self-update: git not found on $(hostname -s), running the existing checkout unmodified"
        return 1
    fi
    local pull_output
    if ! pull_output="$(cd "${REPO_DIR}" && "${GIT_BIN}" pull --ff-only origin main 2>&1)"; then
        _log "[ERROR] self-update: git pull --ff-only origin main failed in ${REPO_DIR}, running the existing checkout unmodified"
        _log "[ERROR] self-update: ${pull_output}"
        return 1
    fi
    _log "[OK] self-update: ${pull_output}"
    return 0
}

#----- alert channels --------------------------------------------------------------

# Each channel returns 0 on delivery, nonzero otherwise, and never aborts the others.
# Primary failures log [ERROR], secondary failures log [WARN]. That distinction is the whole
# point of this section: it is what stops a dead Pushcut key from looking like a dead watchdog.

_alert_email() {
    local subject="$1" body="$2"
    if [ -z "${ALERT_EMAIL}" ]; then
        _log "[ERROR] channel email (PRIMARY): MDT_ALERT_EMAIL is unset, there is no recipient to mail"
        _log "[ERROR] channel email (PRIMARY): export MDT_ALERT_EMAIL=<mailbox>, or set it in the launchd plist's EnvironmentVariables"
        return 1
    fi
    if [ -z "${GWS_BIN}" ]; then
        _log "[ERROR] channel email (PRIMARY): gws not installed on $(hostname -s), cannot mail ${ALERT_EMAIL}"
        _log "[ERROR] channel email (PRIMARY): install with 'brew install googleworkspace-cli', then authenticate"
        return 1
    fi
    _run_command_bounded "${GWS_TIMEOUT_SECONDS}" "${GWS_BIN}" gmail +send \
        --to "${ALERT_EMAIL}" --subject "${subject}" --body "${body}" >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        _log "[ERROR] channel email (PRIMARY): gws gmail +send failed or timed out for ${ALERT_EMAIL}"
        _log "[ERROR] channel email (PRIMARY): verify the configured OAuth login with 'gws auth login --services gmail,drive'"
        return 1
    fi
    _log "[OK] channel email (PRIMARY): sent to ${ALERT_EMAIL}"
    return 0
}

_alert_macos_notification() {
    local title="$1" message="$2"
    if [ ! -x "${OSASCRIPT_BIN}" ]; then
        _log "[WARN] channel macos-notification (secondary): ${OSASCRIPT_BIN} is not executable"
        return 1
    fi
    "${OSASCRIPT_BIN}" -e "display notification \"${message}\" with title \"${title}\"" \
        >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        _log "[WARN] channel macos-notification (secondary): osascript rejected the notification"
        return 1
    fi
    _log "[OK] channel macos-notification (secondary): posted"
    return 0
}

_alert_pushcut() {
    local title="$1" message="$2" api_key response
    if [ -z "${DOPPLER_PROJECT}" ] || [ -z "${DOPPLER_CONFIG}" ] || [ -z "${PUSHCUT_NOTIFICATION}" ]; then
        _log "[WARN] channel pushcut (secondary): UNAVAILABLE; configure MDT_ALERT_DOPPLER_PROJECT, MDT_ALERT_DOPPLER_CONFIG and MDT_ALERT_PUSHCUT_NOTIFICATION explicitly"
        return 1
    fi
    if [ ! -x "${DOPPLER_BIN}" ]; then
        _log "[WARN] channel pushcut (secondary): ${DOPPLER_BIN} is not executable"
        return 1
    fi
    api_key="$("${DOPPLER_BIN}" secrets get PUSHCUT_API_KEY \
        --project "${DOPPLER_PROJECT}" --config "${DOPPLER_CONFIG}" --plain 2>/dev/null)"
    if [ -z "${api_key}" ]; then
        _log "[WARN] channel pushcut (secondary): PUSHCUT_API_KEY absent from ${DOPPLER_PROJECT}/${DOPPLER_CONFIG}"
        return 1
    fi
    response="$("${CURL_BIN}" -s -m "${HTTP_TIMEOUT_SECONDS}" -w '\n%{http_code}' \
        -X POST "https://api.pushcut.io/v1/notifications/$(_urlencode "${PUSHCUT_NOTIFICATION}")" \
        -H "API-Key: ${api_key}" -H 'Content-Type: application/json' \
        -d "$("${PYTHON_BIN}" -c 'import json,sys; print(json.dumps({"title": sys.argv[1], "text": sys.argv[2]}))' \
            "${title}" "${message}")" 2>&1)"
    local status="${response##*$'\n'}"
    if [ "${status}" != "200" ]; then
        _log "[WARN] channel pushcut (secondary): HTTP ${status} from api.pushcut.io"
        return 1
    fi
    _log "[OK] channel pushcut (secondary): delivered"
    return 0
}

_urlencode() {
    "${PYTHON_BIN}" -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1"
}

# Fires every channel, primary first, and returns the PRIMARY channel's verdict alone.
# Secondaries always run, but their failures never determine primary delivery.
_fire_alert_channels() {
    local title="$1" subject="$2" short_message="$3" body="$4"
    local primary_delivered=1 secondaries_delivered=0

    _alert_email "${subject}" "${body}" && primary_delivered=0

    _alert_macos_notification "${title}" "${short_message}" \
        && secondaries_delivered=$((secondaries_delivered + 1))
    _alert_pushcut "${title}" "${short_message}" \
        && secondaries_delivered=$((secondaries_delivered + 1))

    _log "[OK] alert delivery: ${secondaries_delivered} of 2 secondary channels delivered"

    if [ "${primary_delivered}" -ne 0 ]; then
        _log "[ERROR] alert delivery: the PRIMARY email channel did not reach ${ALERT_EMAIL}"
        _log "[ERROR] alert delivery: assume this alert was NOT seen, the secondary channels are not a substitute"
        return 1
    fi
    return 0
}

#----- check orchestration ---------------------------------------------------------

# The one required value with no safe default. Checked BEFORE the health check on
# every run, not only when an alert falls due: a run that discovers its primary
# channel is unconfigured at the moment it needed to page has paged nobody.
_require_alert_email() {
    if [ -z "${ALERT_EMAIL}" ]; then
        _log "[ERROR] precondition: MDT_ALERT_EMAIL is unset, so the PRIMARY alert channel has no recipient"
        _log "[ERROR] precondition: a watchdog that cannot page has not paged. Set MDT_ALERT_EMAIL=<mailbox> in the environment, or in the launchd plist's EnvironmentVariables."
        return 1
    fi
    return 0
}

_run_health_check() {
    _require_alert_email || return 1
    if [ ! -x "${UV_BIN}" ]; then
        _log "[ERROR] precondition: ${UV_BIN} is not executable"
        return 1
    fi
    if [ ! -f "${CHECK_SCRIPT}" ]; then
        _log "[ERROR] precondition: ${CHECK_SCRIPT} not found"
        return 1
    fi
    return 0
}

# Pull one field out of the checker's --json payload. Fails loudly on unparseable input.
_json_field() {
    local payload="$1" expression="$2"
    printf '%s' "${payload}" | "${PYTHON_BIN}" -c "
import json, sys
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError as exc:
    print('unparseable-json: {0}'.format(exc))
    sys.exit(0)
${expression}
"
}

_main() {
    mkdir -p "$(dirname "${LOG_FILE}")"
    _self_update
    _log "[OK] ci-health: starting check for the music-dj-tools repo"

    # Before either path, so a --test run proves the alert path is configured as
    # well as wired: the precondition tests would pass with no recipient at all.
    _require_alert_email || return 1

    if [ "${1:-}" = "--test" ]; then
        _log "[OK] ci-health: TEST MODE, forcing one alert down every channel"
        # Host and timestamp are in the subject so a test mail is attributable to the machine
        # and the run that produced it. Without them, two Macs running this watchdog send
        # byte-identical test mails and neither proves anything about the sender.
        _fire_alert_channels \
            "[TEST] mdt-ci-health" \
            "[TEST] mdt-ci-health alert channel check from $(hostname -s) at $(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
            "Test alert. The CI health watchdog alert path is wired up correctly." \
            "This is a TEST alert from the music-dj-tools CI health watchdog.

It proves the alert path works end to end. No CI problem was detected.

Classification: test
Remediation:    none required, this is a channel test

Sent from: $(hostname -s) at $(date -u '+%Y-%m-%dT%H:%M:%SZ')
Watchdog:  scripts/ci_health_notify.sh --test
Checker:   scripts/ci_health_check.py
Schedule:  ~/Library/LaunchAgents/com.YOU.mdt-ci-health.plist (every 4 hours)
Log:       ${LOG_FILE}"
        return $?
    fi

    _run_health_check || return 1

    local json_output exit_code
    # Run as a MODULE from the repo root, not as a loose script path. The checker imports
    # scripts.ci_health_core and scripts.ci_health_metrics, so it needs the repo on
    # sys.path, and `python -m` from REPO_DIR is what puts it there. `--no-project` is kept
    # deliberately: the watchdog is stdlib-only and must keep running even when the
    # project's own venv or an optional extra is broken, which is precisely when CI health
    # is worth knowing.
    json_output="$(cd "${REPO_DIR}" && BOUNDED_COMMAND_TIMEOUT_JSON=1 _run_command_bounded \
        "${HEALTH_CHECK_TIMEOUT_SECONDS}" "${UV_BIN}" run --no-project python -m \
        scripts.ci_health_check --json 2>&1)"
    exit_code=$?

    if [ "${exit_code}" -eq 124 ]; then
        _log "[ERROR] ci-health: checker timed out after ${HEALTH_CHECK_TIMEOUT_SECONDS}s; terminal timeout receipt follows"
    fi

    if [ "${exit_code}" -eq 0 ]; then
        _log "[OK] ci-health: all checks passed, no alert needed"
        return 0
    fi

    local classification remediation detail
    classification="$(_json_field "${json_output}" \
        "print(next((c['classification'] for c in data.get('checks', []) if not c['ok']), data.get('error', 'unknown')))")"
    detail="$(_json_field "${json_output}" \
        "print(next((c['detail'] for c in data.get('checks', []) if not c['ok']), data.get('error', 'no detail')))")"
    remediation="$(_json_field "${json_output}" \
        "print(next((c['remediation'] for c in data.get('checks', []) if not c['ok'] and c['remediation']), 'See scripts/ci_health_check.py exit codes.'))")"

    _log "[ERROR] ci-health: ${classification} (exit ${exit_code})"
    _log "[ERROR] ci-health: ${detail}"
    _log "[ERROR] ci-health: remediation: ${remediation}"

    _fire_alert_channels \
        "mdt-ci-health: ${classification}" \
        "[ALERT] mdt-ci-health: ${classification}" \
        "${detail}" \
        "The music-dj-tools CI health watchdog found a problem.

Classification: ${classification} (exit ${exit_code})
Detail:         ${detail}
Remediation:    ${remediation}
Sent from:      $(hostname -s)

Full result:
${json_output}

Checker:   scripts/ci_health_check.py
Schedule:  ~/Library/LaunchAgents/com.YOU.mdt-ci-health.plist (every 4 hours)
Log:       ${LOG_FILE}"

    return "${exit_code}"
}

_main "$@"
