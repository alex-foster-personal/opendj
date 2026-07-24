#!/bin/bash
# Poll mem_gate and launch the next split wave on the first OK.
# WHY POLL: a single check is BLOCK ~88% of the time (1423-sample history), but
# calm runs reach 78 consecutive minutes against a 22-minute job. Checking once
# tells you almost nothing; polling costs a minute and buys the one resource
# that actually kills a run.
cd /Users/dev/Music/music-dj-tools
MAX=${MAX_POLLS:-180}   # 3h at 60s
for i in $(seq 1 "$MAX"); do
  OUT=$(uv run scripts/mem_gate.py check 2>&1 | tail -1)
  if uv run scripts/mem_gate.py check >/dev/null 2>&1; then
    echo "[$(date +%H:%M:%S)] poll $i: $OUT -> LAUNCHING"
    exec uv run --with modal python -m scripts.stem_split_runner --limit 100 --dest bifrost2
  fi
  echo "[$(date +%H:%M:%S)] poll $i: $OUT"
  sleep 60
done
echo "gave up after $MAX polls; machine never cleared"
exit 2
