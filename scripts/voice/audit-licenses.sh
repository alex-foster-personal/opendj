#!/usr/bin/env bash
# C9 licence audit: fail if the default install pulls any GPL / AGPL
# dependency into the in-process runtime.
#
# Phase 14 / VOICE-01. Runs against the active venv.
set -euo pipefail

# pip-licenses is the standard tool; we tolerate its absence by emitting
# a clear hint rather than failing on the audit itself.
if ! command -v pip-licenses >/dev/null 2>&1; then
  echo "[voice] pip-licenses not installed; \`pip install pip-licenses\` to run audit" >&2
  exit 2
fi

TMPFILE="$(mktemp -t voice-licenses-XXXXXX)"
trap 'rm -f "${TMPFILE}"' EXIT

pip-licenses --format=plain --with-system --with-license-file=false \
  --ignore-packages pip setuptools wheel \
  > "${TMPFILE}"

# Any GPL variant is a fail. Porcupine is proprietary (not copyleft) so
# we separately warn rather than fail if the user opted in. The audit
# default is the C9 permissive set.
BAD="$(grep -iE 'GPL|AGPL' "${TMPFILE}" || true)"
if [[ -n "${BAD}" ]]; then
  echo "[voice] GPL/AGPL-licensed dependency detected -- default build must stay permissive" >&2
  echo "${BAD}" >&2
  exit 1
fi

# Pvporcupine warning (opt-in, proprietary).
if grep -iE 'pvporcupine|porcupine' "${TMPFILE}" >/dev/null 2>&1; then
  echo "[voice] NOTE: pvporcupine (proprietary) detected in the env." >&2
  echo "[voice] This is allowed under WAKE_BACKEND=porcupine but the default build should not install it." >&2
fi

echo "[voice] licence audit passed (no GPL / AGPL in default install)"
exit 0
