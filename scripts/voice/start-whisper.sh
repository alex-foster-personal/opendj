#!/usr/bin/env bash
# Start the whisper.cpp warm HTTP daemon on port 2022.
#
# Requirements:
#   - whisper-cpp (Homebrew) or a custom build under vendor/whisper.cpp.
#   - ggml-small.en.bin (see apps/voice/MODELS.md).
#
# Respects env:
#   VOICE_WHISPER_MODEL  path to .bin (default: models/ggml-small.en.bin)
#   VOICE_WHISPER_PORT   port to bind (default: 2022)
#   VOICE_WHISPER_BIN    explicit binary override
#
# Phase 14 / VOICE-01.
set -euo pipefail

PORT="${VOICE_WHISPER_PORT:-2022}"
MODEL="${VOICE_WHISPER_MODEL:-models/ggml-small.en.bin}"
BIN="${VOICE_WHISPER_BIN:-}"

# Try common binary names if not given explicitly.
if [[ -z "${BIN}" ]]; then
  for candidate in whisper-server whisper-cpp-server; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      BIN="${candidate}"
      break
    fi
  done
fi

if [[ -z "${BIN}" ]]; then
  echo "[voice] could not find whisper-server or whisper-cpp-server" >&2
  echo "[voice] install via 'brew install whisper-cpp' or build from vendor/whisper.cpp" >&2
  echo "[voice] see apps/voice/MODELS.md for details" >&2
  exit 1
fi

if [[ ! -f "${MODEL}" ]]; then
  echo "[voice] model weights missing at ${MODEL}" >&2
  echo "[voice] run 'bash ./models/download-ggml-model.sh small.en' from the whisper.cpp checkout" >&2
  exit 1
fi

echo "[voice] starting ${BIN} -m ${MODEL} on port ${PORT}"
exec "${BIN}" \
  -m "${MODEL}" \
  --port "${PORT}" \
  --inference-path /inference \
  --host 127.0.0.1 \
  --threads 4
