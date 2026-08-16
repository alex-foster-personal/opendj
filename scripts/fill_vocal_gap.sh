#!/usr/bin/env bash
# Demucs-fill vocal-cache for the fillable top-played-playlist gap tracks.
# Reads .tmp/vocal_gap_fillable.json, runs the vocal worker per stable_id,
# logs N/total + resulting status. Self-contained; safe to re-run (valid
# cache is skipped unless --force).
set -uo pipefail
REPO="/Users/dev/Music/music-dj-tools"
cd "$REPO"
LOG="$REPO/.tmp/vocal_fill.log"
# htdemucs hits a hard MPS limit (Output channels > 65536) that
# PYTORCH_ENABLE_MPS_FALLBACK cannot rescue (it's a dimension cap, not a
# missing-op), so MPS always bails to CPU with a per-track exception. Force
# CPU up front: no wasted MPS attempt, no confusing WARN spam, same speed.
export MDT_VOCAL_WORKER_DEVICE="${MDT_VOCAL_WORKER_DEVICE:-cpu}"

# stable_ids are hex (no spaces) -> safe to word-split (bash 3.2 has no mapfile)
IDS=$(python3 -c "import json;[print(g['stable_id']) for g in json.load(open('.tmp/vocal_gap_fillable.json'))]")
N=$(printf '%s\n' "$IDS" | grep -c .)
echo "[$(date '+%H:%M:%S')] START vocal fill: $N tracks, device=$MDT_VOCAL_WORKER_DEVICE" | tee "$LOG"

i=0; ok=0; fail=0
for sid in $IDS; do
  i=$((i+1))
  title=$(sqlite3 data/state/state.db "select title from tracks where stable_id='$sid';" 2>/dev/null)
  echo "[$(date '+%H:%M:%S')] ($i/$N) $sid  $title" >> "$LOG"
  if .venv/bin/python -m apps.vocals one --stable-id "$sid" \
        --data-dir "$REPO/data" --worker-timeout-s 1500 >> "$LOG" 2>&1; then
    st=$(.venv/bin/python - "$sid" <<'PY' 2>/dev/null
import sys,json,glob,os
sid=sys.argv[1]; p=f"data/state/vocal-cache/{sid}.json"
if os.path.exists(p):
    d=json.load(open(p)); print(d.get("status") or ("regions:"+str(len(d.get("regions",[])))))
else: print("NO-CACHE")
PY
)
    echo "[$(date '+%H:%M:%S')] ($i/$N) OK -> $st" >> "$LOG"; ok=$((ok+1))
  else
    echo "[$(date '+%H:%M:%S')] ($i/$N) FAILED" >> "$LOG"; fail=$((fail+1))
  fi
done
echo "[$(date '+%H:%M:%S')] DONE: ok=$ok fail=$fail of $N" | tee -a "$LOG"
