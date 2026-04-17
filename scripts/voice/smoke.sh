#!/usr/bin/env bash
# Plan 1 smoke: measure grammar+dispatch wall-clock time via the
# `python -m apps.voice bench` CLI. No mic, no whisper daemon needed.
#
# Phase 14 / VOICE-01.
set -euo pipefail

ITER="${1:-50}"
TEXT="${2:-find daft punk}"

cd "$(git rev-parse --show-toplevel)"
exec python -m apps.voice bench --iterations "${ITER}" --text "${TEXT}"
