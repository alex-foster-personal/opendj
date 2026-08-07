#!/usr/bin/env bash
# Fire batch 1 on whatever is present NOW, then a second batch on stragglers
# as they land. Both stems kept -> stems/; every vocal flat-copied -> vocals/.
set -uo pipefail
BASE="/Users/dev/Music/_incoming/clubsauna-acapella-techno-100"
REPO="/Users/dev/Music/music-dj-tools"
cd "$REPO"
exec >>"$BASE/farm.log" 2>&1

audio() { find "$BASE" -maxdepth 1 -type f \( -iname '*.mp3' -o -iname '*.wav' \
  -o -iname '*.flac' -o -iname '*.m4a' -o -iname '*.aif' -o -iname '*.aiff' \) | sort; }
flat_vocals() {
  find "$BASE/stems" -type f -iname 'vocals.*' 2>/dev/null | while read -r v; do
    slug="$(basename "$(dirname "$v")")"; ext="${v##*.}"
    d="$BASE/vocals/${slug}.vocals.${ext}"; [ -f "$d" ] || cp "$v" "$d"
  done
}
fire() { # $1 run-id, remaining args = input files
  local rid="$1"; shift
  echo "[$(date '+%H:%M:%S')] FIRE $rid over $# tracks (max_containers=60)"
  MDT_ROFORMER_MAX_CONTAINERS=60 uv run --with modal python scripts/modal_roformer_spike.py \
    separate --input "$@" --out-dir "$BASE/stems" --run-id "$rid"
  echo "[$(date '+%H:%M:%S')] $rid returned rc=$?"
}

# stream vocals -> flat dir continuously, across both batches
( while :; do flat_vocals; sleep 8; done ) & CPWATCH=$!
trap 'kill "$CPWATCH" 2>/dev/null' EXIT

# ---- BATCH 1: everything present now ----
B1="$BASE/.batch1.list"; audio > "$B1"
N1=$(wc -l < "$B1" | tr -d ' ')
echo "[$(date '+%H:%M:%S')] BATCH 1 = $N1 tracks"
# shellcheck disable=SC2046
fire "clubsauna-acapella-b1" $(cat "$B1")

# ---- BATCH 2: stragglers not in batch 1 ----
echo "[$(date '+%H:%M:%S')] batch 1 done; waiting for stragglers (deadline 25m)"
deadline=$(( $(date +%s) + 1500 )); prevsig=""; stable=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  NEW=$(audio | grep -vxF -f "$B1" || true)
  total=$(audio | wc -l | tr -d ' ')
  sig=$(printf '%s' "$NEW" | md5)
  if [ "$sig" = "$prevsig" ]; then stable=$((stable+1)); else stable=0; fi
  echo "[$(date '+%H:%M:%S')] total=$total new=$(printf '%s' "$NEW" | grep -c . || true) stable=$stable"
  if [ -n "$NEW" ] && { [ "$total" -ge 100 ] || [ "$stable" -ge 4 ]; }; then break; fi
  [ "$total" -ge 100 ] && [ -z "$NEW" ] && break
  prevsig="$sig"; sleep 15
done
NEW=$(audio | grep -vxF -f "$B1" || true)
if [ -n "$NEW" ]; then
  # shellcheck disable=SC2046
  fire "clubsauna-acapella-b2" $NEW
else
  echo "[$(date '+%H:%M:%S')] no stragglers to batch"
fi

sleep 10; flat_vocals
echo "[$(date '+%H:%M:%S')] ALL DONE | vocals_flat=$(find "$BASE/vocals" -type f | wc -l | tr -d ' ') | stem_dirs=$(find "$BASE/stems" -type d -mindepth 2 -maxdepth 2 | wc -l | tr -d ' ') | audio_total=$(audio | wc -l | tr -d ' ')"
