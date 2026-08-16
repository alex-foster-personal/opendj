#!/usr/bin/env bash
# Watch the incoming dir until the drop settles, then fire ONE wide roformer
# H100 batch over all audio, streaming stems -> stems/ and copying each vocal
# into a flat vocals/ dir as it lands. Both stems kept; vocals also flat-copied.
set -uo pipefail
BASE="/Users/dev/Music/_incoming/clubsauna-acapella-techno-100"
REPO="/Users/dev/Music/music-dj-tools"
LOG="$BASE/farm.log"
cd "$REPO"
exec >>"$LOG" 2>&1

audio() { find "$BASE" -maxdepth 1 -type f \( -iname '*.mp3' -o -iname '*.wav' \
  -o -iname '*.flac' -o -iname '*.m4a' -o -iname '*.aif' -o -iname '*.aiff' \) | sort; }
sig() { audio | while read -r f; do printf '%s %s\n' "$(stat -f '%z' "$f")" "$f"; done | md5; }
flat_vocals() {
  find "$BASE/stems" -type f -iname 'vocals.*' 2>/dev/null | while read -r v; do
    slug="$(basename "$(dirname "$v")")"; ext="${v##*.}"
    dest="$BASE/vocals/${slug}.vocals.${ext}"
    [ -f "$dest" ] || cp "$v" "$dest"
  done
}

echo "[$(date '+%H:%M:%S')] watcher start"
prev=""; stable=0
while true; do
  cnt="$(audio | wc -l | tr -d ' ')"; cur="$(sig)"
  if [ "$cur" = "$prev" ]; then stable=$((stable+1)); else stable=0; fi
  echo "[$(date '+%H:%M:%S')] audio=$cnt stable=$stable"
  [ "$cnt" -ge 100 ] && { echo "  reached 100"; break; }
  [ "$stable" -ge 4 ] && [ "$cnt" -gt 0 ] && { echo "  settled (no change 60s)"; break; }
  prev="$cur"; sleep 15
done

FILES=$(audio)
N=$(printf '%s\n' "$FILES" | wc -l | tr -d ' ')
echo "[$(date '+%H:%M:%S')] FIRING roformer H100 farm over $N tracks (max_containers=60)"

# stream vocals -> flat dir while the farm runs
( while :; do flat_vocals; sleep 10; done ) & CPWATCH=$!

# filenames are NNN_<id>.<ext> with no spaces -> safe to word-split
MDT_ROFORMER_MAX_CONTAINERS=60 uv run --with modal python scripts/modal_roformer_spike.py \
  separate --input $FILES --out-dir "$BASE/stems" --run-id clubsauna-acapella-100
RC=$?

sleep 12; kill "$CPWATCH" 2>/dev/null; flat_vocals
VOX=$(find "$BASE/vocals" -type f | wc -l | tr -d ' ')
DIRS=$(find "$BASE/stems" -type d -mindepth 2 -maxdepth 2 2>/dev/null | wc -l | tr -d ' ')
echo "[$(date '+%H:%M:%S')] DONE rc=$RC | vocals_flat=$VOX | stem_track_dirs=$DIRS"
