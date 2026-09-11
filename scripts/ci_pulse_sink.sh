#!/usr/bin/env bash
#
# ci_pulse_sink.sh -- give an e2e job a null audio sink on the host's shared
# PulseAudio server, and put the server back afterwards.
#
#   scripts/ci_host_lock.sh pulse-sink scripts/ci_pulse_sink.sh setup
#   scripts/ci_host_lock.sh pulse-sink scripts/ci_pulse_sink.sh restore
#
# The server is shared by every job of this user on the host (WSLg's server on
# nucbox, one per-user daemon on agentbox), and two jobs can be in here at once,
# so both halves run under the `pulse-sink` host lock: the ownership check and
# the mutation are one critical section, not a check followed by a race. The
# lock is held only for these few pactl calls, never for the suites.
#
# setup:   probe the SOCKET with `pactl info` (`pulseaudio --check` reads the
#          pid file and is a documented false negative under WSLg, where the
#          pid link dangles while the server answers), start one only if
#          nothing answers, load a null sink named after THIS runner and run,
#          make it the default, prove it, export PULSE_SINK so this job's
#          clients bind to it regardless of the server default, and record
#          sink/module/previous default in $GITHUB_ENV for restore.
# restore: unload only our module; put the default back only while it is still
#          our sink, and only to a DURABLE sink: the recorded previous default
#          may be another job's transient mdt-ci-* sink already unloaded, so
#          fall back to the first non-CI sink on the server.
#
# Regression lines:
#   - if setup starts a daemon while a server already answers then broken on WSLg
#   - if two jobs' sinks share a name, or one job unloads the other's module,
#     then broken
#   - if restore ever sets the default to an mdt-ci-* sink, or changes a default
#     that is no longer ours, then broken
set -euo pipefail

readonly MODE="${1:?usage: ci_pulse_sink.sh setup|restore}"
readonly RUNNER="${RUNNER_NAME:?RUNNER_NAME is unset}"
# A sink name is a PulseAudio registered name: keep [A-Za-z0-9_.-], map the
# rest (hosted runners are named like "GitHub Actions 2") to underscores, and
# keep the job's run/attempt so two jobs on one hosted-style name stay apart.
SAFE_RUNNER="$(printf '%s' "$RUNNER" | tr -c 'A-Za-z0-9_.-' '_')"
readonly SINK="mdt-ci-${SAFE_RUNNER}-${GITHUB_RUN_ID:-0}-${GITHUB_RUN_ATTEMPT:-0}"
readonly ENV_FILE="${GITHUB_ENV:-/dev/null}"

_default_sink() { pactl get-default-sink 2>/dev/null || true; }
_sinks() { pactl list short sinks 2>/dev/null | awk '{print $2}'; }

setup() {
    if pactl info >/dev/null 2>&1; then
        echo "[OK] PulseAudio server already answering: $(pactl info | grep 'Server String')"
    else
        pulseaudio --start --exit-idle-time=-1
    fi
    local prev module
    prev="$(_default_sink)"
    # A server that `--start` just brought up can refuse its first module for
    # a moment ("Module initialization failed"); three attempts, one second apart.
    local attempt=0
    until module="$(pactl load-module module-null-sink "sink_name=$SINK" 2>&1)" && [[ "$module" =~ ^[0-9]+$ ]]; do
        attempt=$((attempt + 1))
        [ "$attempt" -lt 3 ] || { echo "[ERROR] could not load null sink $SINK: $module"; exit 1; }
        sleep 1
    done
    # Persist what restore needs the moment the module exists, BEFORE anything
    # below can fail: a red set-default or proof must still leave the always-run
    # restore able to unload this sink. PULSE_SINK pins every client this job
    # starts to ITS sink, whatever the server-wide default is by the time a
    # browser opens audio (a concurrent job may have moved the default since).
    { echo "MDT_PULSE_SINK=$SINK"; echo "MDT_PULSE_PREV_SINK=$prev"; echo "MDT_PULSE_NULL_MODULE=$module"; echo "PULSE_SINK=$SINK"; } >> "$ENV_FILE"
    pactl set-default-sink "$SINK"
    [ "$(_default_sink)" = "$SINK" ] || { echo "[ERROR] $SINK is not the default sink"; pactl info; exit 1; }
    echo "[OK] default sink $SINK (module $module, previous '${prev:-none}')"
}

restore() {
    local sink="${MDT_PULSE_SINK:-$SINK}" module="${MDT_PULSE_NULL_MODULE:-}" prev="${MDT_PULSE_PREV_SINK:-}" target
    if [ "$(_default_sink)" = "$sink" ]; then
        target="$prev"
        case "$target" in mdt-ci-*|"") target="";; esac
        if [ -z "$target" ] || ! _sinks | grep -qx "$target"; then
            target="$(_sinks | grep -v '^mdt-ci-' | head -1 || true)"
        fi
        if [ -n "$target" ]; then
            pactl set-default-sink "$target" || echo "[WARN] could not restore default sink $target"
        else
            echo "[WARN] no durable sink to restore; leaving the default on $sink"
        fi
    fi
    if [ -n "$module" ]; then
        pactl unload-module "$module" || echo "[WARN] module $module was already gone"
    fi
    echo "[OK] pulse restored: default=$(_default_sink)"
}

case "$MODE" in
    setup) setup ;;
    restore) restore ;;
    *) echo "[ERROR] usage: ci_pulse_sink.sh setup|restore" >&2; exit 2 ;;
esac
