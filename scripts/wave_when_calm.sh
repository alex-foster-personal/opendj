#!/bin/bash
# Poll mem_gate and launch the next split wave on the first OK.
# WHY POLL: a single check is BLOCK ~88% of the time (1423-sample history), but
# calm runs reach 78 consecutive minutes against a 22-minute job. Checking once
# tells you almost nothing; polling costs a minute and buys the one resource
# that actually kills a run.
set -euo pipefail
# This repo's own checkout location, not a machine-specific value (#910): no
# personal home should ever be hardcoded here.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# An unguarded `cd` here would poll, and then LAUNCH A MODAL RUN, from whatever
# directory the caller happened to be in. Explicit so it cannot be silent.
cd "$REPO" || { echo "FATAL: repo checkout missing" >&2; exit 1; }
MAX=${MAX_POLLS:-180}   # 3h at 60s
for i in $(seq 1 "$MAX"); do
  # BLOCK is the EXPECTED answer ~88% of the time, so a nonzero gate status is
  # data, not an error: tolerate it explicitly rather than letting -e end the
  # poll on the first busy sample. The launch decision is the `if` below, which
  # reads the gate's own status un-piped.
  OUT=$(uv run --no-sync scripts/mem_gate.py check 2>&1 | tail -1) || true
  if uv run --no-sync scripts/mem_gate.py check >/dev/null 2>&1; then
    echo "[$(date +%H:%M:%S)] poll $i: $OUT -> LAUNCHING"
    exec uv run --no-sync --with modal python -m scripts.stem_split_runner --limit 100 --dest bifrost2
  fi
  echo "[$(date +%H:%M:%S)] poll $i: $OUT"
  sleep 60
done
echo "gave up after $MAX polls; machine never cleared"
exit 2
