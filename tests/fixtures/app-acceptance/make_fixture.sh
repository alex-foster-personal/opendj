#!/usr/bin/env bash
# Regenerates the installed-app acceptance fixture library (OPS-54).
#
# Every byte is synthesized here: ffmpeg lavfi tones for the beds and macOS
# `say` for the one vocal line, so the fixture carries no third-party audio.
# The committed mp3s are the output of this script; fixture.json records the
# expected counts the acceptance asserts. Run on macOS (needs `say`) with
# ffmpeg on PATH:  tests/fixtures/app-acceptance/make_fixture.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$here/library"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$out"

# A four-on-the-floor kick (decaying 55 Hz burst every beat) under a bass tone.
bed() { # <bpm> <bass_hz> <seconds> <out.wav>
  local beat
  beat=$(awk "BEGIN{print 60/$1}")
  ffmpeg -v error -y \
    -f lavfi -i "aevalsrc='0.8*sin(2*PI*55*t)*exp(-18*mod(t,${beat}))':s=44100:d=${3}" \
    -f lavfi -i "sine=frequency=${2}:sample_rate=44100:duration=${3}" \
    -filter_complex "[1]volume=0.25[b];[0][b]amix=inputs=2:normalize=0,aformat=channel_layouts=stereo" \
    "$4"
}
tag_mp3() { # <in.wav> <title> <out.mp3>
  ffmpeg -v error -y -i "$1" -codec:a libmp3lame -b:a 96k \
    -metadata title="$2" -metadata artist="Open DJ Fixture" \
    -metadata album="Acceptance" "$3"
}

bed 124 110 20 "$work/pulse.wav"
tag_mp3 "$work/pulse.wav" "Acceptance Pulse" "$out/01-acceptance-pulse.mp3"

bed 100 147 20 "$work/drift.wav"
tag_mp3 "$work/drift.wav" "Acceptance Drift" "$out/02-acceptance-drift.mp3"

# The vocal track: the stems + lyrics subject. Plain spoken words, written for
# this fixture, so a lyrics pipeline has something real to transcribe.
say -v Samantha -r 150 -o "$work/voice.aiff" \
  "Open D J acceptance. The installed app must play this voice. Open D J acceptance. Every build is tested before it ships."
ffmpeg -v error -y -i "$work/voice.aiff" -ar 44100 -ac 2 -af "apad=whole_dur=30" "$work/voice.wav"
bed 96 98 30 "$work/vbed.wav"
ffmpeg -v error -y -i "$work/vbed.wav" -i "$work/voice.wav" \
  -filter_complex "[0]volume=0.6[a];[1]volume=1.4[v];[a][v]amix=inputs=2:normalize=0" "$work/vocal.wav"
tag_mp3 "$work/vocal.wav" "Acceptance Voice" "$out/03-acceptance-voice.mp3"

# Known-answer analysis tracks (OPS-60). Durations and digests are recorded
# in fixture.json; a regen changes those bytes, so update the digests with it.
mkdir -p "$here/corrupt"
beat124=$(awk "BEGIN{printf \"%.8f\", 60/124}")
bar124=$(awk "BEGIN{printf \"%.8f\", 240/124}")
ffmpeg -v error -y \
  -f lavfi -i "aevalsrc='0.35*sin(2*PI*55*t)*exp(-20*mod(t,${beat124}))':s=44100:d=60" \
  -f lavfi -i "aevalsrc='1.0*sin(2*PI*48*t)*exp(-10*mod(t,${bar124}))':s=44100:d=60" \
  -filter_complex "[0][1]amix=inputs=2:normalize=0,aformat=channel_layouts=stereo" \
  "$work/kick124.wav"
tag_mp3 "$work/kick124.wav" "Acceptance Kick 124" "$out/04-acceptance-kick-124.mp3"

ffmpeg -v error -y \
  -f lavfi -i "sine=frequency=220:sample_rate=44100:duration=45" \
  -f lavfi -i "sine=frequency=261.6256:sample_rate=44100:duration=45" \
  -f lavfi -i "sine=frequency=329.6276:sample_rate=44100:duration=45" \
  -filter_complex "[0][1][2]amix=inputs=3:normalize=0,volume=0.5,aformat=channel_layouts=stereo" \
  "$work/aminor.wav"
tag_mp3 "$work/aminor.wav" "Acceptance A Minor" "$out/05-acceptance-a-minor.mp3"

beat90=$(awk "BEGIN{printf \"%.8f\", 60/90}")
bar90=$(awk "BEGIN{printf \"%.8f\", 240/90}")
ffmpeg -v error -y \
  -f lavfi -i "aevalsrc='0.30*sin(2*PI*2000*t)*exp(-80*mod(t,${beat90}))':s=44100:d=60" \
  -f lavfi -i "aevalsrc='1.0*sin(2*PI*1500*t)*exp(-40*mod(t,${bar90}))':s=44100:d=60" \
  -filter_complex "[0][1]amix=inputs=2:normalize=0,aformat=channel_layouts=stereo" \
  "$work/click90.wav"
tag_mp3 "$work/click90.wav" "Acceptance Click 90" "$out/06-acceptance-click-90.mp3"

ffmpeg -v error -y -f lavfi -i "sine=frequency=440:sample_rate=44100:duration=2" \
  -ac 2 "$work/carrier.wav"
tag_mp3 "$work/carrier.wav" "Acceptance Corrupt" "$out/07-acceptance-corrupt-carrier.mp3"
# The negative control is this carrier with its tail cut off. Import rejects
# it (the tag still claims 2 s), so the harness swaps it in after import.
dd if="$out/07-acceptance-corrupt-carrier.mp3" \
  of="$here/corrupt/99-acceptance-truncated.mp3" bs=4096 count=1 >/dev/null 2>&1

ls -l "$out" "$here/corrupt"
