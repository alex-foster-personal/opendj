#!/usr/bin/env bash
# make-phase7-fixtures.sh -- regenerate the Phase 7 dedup/tag fixture set.
#
# Creates deterministic tiny audio files under tests/fixtures/phase7-dedup
# using ffmpeg's built-in signal generators. We never ship real music
# (copyright) and never rely on external downloads.
#
# Fixtures:
#   src.wav          -- 5 s 440 Hz sine + white noise (anchor)
#   src-320.mp3      -- same content @ 320 kbps
#   src-128.mp3      -- same content @ 128 kbps
#   src-v2.mp3       -- same content @ 192 kbps with ID3v2.4 tags
#   src.m4a          -- same content @ AAC 192 kbps
#   src.flac         -- same content lossless FLAC
#   other-silent-intro.mp3 -- 10 s silence + different content (negative)
#
# Usage: scripts/make-phase7-fixtures.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DEST="$REPO_ROOT/tests/fixtures/phase7-dedup"
mkdir -p "$DEST"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[make-phase7-fixtures] ffmpeg required; install with brew install ffmpeg" >&2
    exit 1
fi

# Seeded signal: 5 s of 440 Hz sine mixed with low-level pink noise.
common="-y -hide_banner -loglevel error"

# Generate the WAV anchor first; everything else transcodes from it so
# fingerprints stay close across bitrates.
ffmpeg $common -f lavfi -i "sine=frequency=440:duration=3,aformat=sample_rates=22050:channel_layouts=mono" \
    -acodec pcm_s16le "$DEST/src.wav"

ffmpeg $common -i "$DEST/src.wav" -ab 320k "$DEST/src-320.mp3"
ffmpeg $common -i "$DEST/src.wav" -ab 128k "$DEST/src-128.mp3"
ffmpeg $common -i "$DEST/src.wav" -ab 192k -metadata title="Source V2" -metadata artist="Fixture" \
    "$DEST/src-v2.mp3"
ffmpeg $common -i "$DEST/src.wav" -c:a aac -b:a 192k "$DEST/src.m4a"
ffmpeg $common -i "$DEST/src.wav" -c:a flac "$DEST/src.flac"

# Negative fixture: different source (200 Hz tone + 3 s silence prefix).
ffmpeg $common -f lavfi -i "anullsrc=duration=3:r=22050:cl=mono" \
    -f lavfi -i "sine=frequency=200:duration=3,aformat=sample_rates=22050:channel_layouts=mono" \
    -filter_complex "[0:a][1:a]concat=n=2:v=0:a=1" \
    -ab 96k "$DEST/other-silent-intro.mp3"

echo "[make-phase7-fixtures] wrote:"
ls -la "$DEST"
