#!/usr/bin/env bash
# One-command voice daemon launcher.
#
# 1. Start the whisper.cpp warm HTTP daemon in the background.
# 2. Start `python -m apps.voice run` in the foreground.
# Shutting down with ^C takes both services down.
#
# Phase 14 / VOICE-01.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

ROOT="$(pwd)"
WHISPER_LOG="${ROOT}/data/voice/whisper-server.log"
mkdir -p "$(dirname "${WHISPER_LOG}")"

echo "[voice] starting whisper.cpp daemon in background -> ${WHISPER_LOG}"
"${ROOT}/scripts/voice/start-whisper.sh" > "${WHISPER_LOG}" 2>&1 &
WHISPER_PID=$!

cleanup() {
  echo "[voice] shutting down (whisper pid=${WHISPER_PID})"
  kill "${WHISPER_PID}" 2>/dev/null || true
  wait "${WHISPER_PID}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# Give the daemon a second to bind the port.
sleep 1

echo "[voice] starting python -m apps.voice run $*"
exec "${ROOT}/.venv/bin/python" -m apps.voice run "$@"
