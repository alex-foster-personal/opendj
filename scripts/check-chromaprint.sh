#!/usr/bin/env bash
# check-chromaprint.sh -- verify the chromaprint fpcalc CLI is available.
#
# Phase 7 (dedup) needs `fpcalc` on PATH to compute acoustic fingerprints
# for cross-bitrate dedup. pyacoustid uses fpcalc under the hood; when it
# is missing, acoustid.fingerprint_file raises NoBackendError.
#
# Exit codes:
#   0  -- fpcalc found, prints version
#   1  -- fpcalc missing; prints remediation
#
# Usage:
#   scripts/check-chromaprint.sh          # quiet success, loud failure
#   scripts/check-chromaprint.sh --loud   # always print version
set -euo pipefail

if ! command -v fpcalc >/dev/null 2>&1; then
    cat >&2 <<'EOF'
[check-chromaprint] ERROR: fpcalc not found on PATH.

Phase 7 (dedup) requires the Chromaprint CLI. Install it with:

    brew install chromaprint

Then re-run this script to verify. If you use MacPorts or Linux, see
docs/install-notes.md for alternatives.
EOF
    exit 1
fi

if [[ "${1:-}" == "--loud" ]]; then
    fpcalc -version
else
    fpcalc -version >/dev/null 2>&1
    echo "[check-chromaprint] ok: $(fpcalc -version 2>&1 | head -1)"
fi
