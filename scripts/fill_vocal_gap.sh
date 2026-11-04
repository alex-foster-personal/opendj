#!/usr/bin/env bash
# Supersedes: the JSON one-loop overnight runner (issue #165).
# Loud tombstone so bookmarks and old cron entries fail closed.
set -uo pipefail
cat >&2 <<'EOF'
FATAL: scripts/fill_vocal_gap.sh is superseded by:
  MDT_VOCAL_WORKER_DEVICE=cpu python -m apps.vocals trickle --live --max-minutes N --data-dir <data>
Keep MDT_VOCAL_WORKER_DEVICE=cpu (htdemucs hits a hard MPS channel cap).
EOF
exit 2
