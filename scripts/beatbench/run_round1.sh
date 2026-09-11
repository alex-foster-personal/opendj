#!/usr/bin/env bash
# Beat-mapping benchmark, round 1.
#
# TWO COMMANDS, AND THE MODE IS REQUIRED. There is no default, because the two
# differ by roughly eleven hours and a silent default would eventually be the
# wrong one:
#
#   scripts/beatbench/run_round1.sh reproduce      [BASE_URL]
#       Reproduces exactly what is committed under ops/beatbench/round-1: the
#       MODEL arm over the 90-fixture subset named in fixtures-subset90.json,
#       and the librosa and constant-128 arms over all 337. Roughly 25 minutes
#       of model time on an unloaded host.
#
#   scripts/beatbench/run_round1.sh full-model-arm [BASE_URL]
#       The arm round 1 could not afford: the model over all 337 fixtures,
#       about 11 hours serial. Writes to its OWN -full337 filenames so it can
#       never overwrite the committed 90-track arm, and produces a separate
#       report. Nothing under ops/beatbench/round-1 that is committed today
#       came from this command.
#
# WHY THE MODEL ARM IS A SUBSET AT ALL. The host carried a load average of 554
# with roughly 71 MB free while round 1 ran, and the model read 2.78x realtime
# against round 0's 33.8x. The committed subset is the honest denominator; the
# byte-identity control in the PR body is what says the 90 and the 337 are the
# same measurement rather than a convenient sample.
#
# NOT A justfile RECIPE ON PURPOSE: the justfile is a declared hotspot file with
# a single owner per wave (CLAUDE.md fan-out conventions), and this lane does not
# own it. Folding this in is a one-line follow-up for whoever does.
#
# WHAT ROUND 1 CHANGES FROM ROUND 0, and nothing else:
#   - scorer v1.1.0 (least-squares BPM, CMLt/AMLt, shift-corrected F)
#   - beat-this pinned to the 1.1.0 PyPI release, run through the LANE producer
#     rather than a bench-only copy of it
#   - bar numbers assigned by apps/analysis_beatgrid/bar_phase.py rather than
#     beat_this's unbounded counter
#   - a constant-BPM floor
# The fixture SET is round 0's, re-decoded at the recorded windows, so a delta
# between the rounds is attributable to those things.
#
# THE 0.35-THRESHOLD ARM IS NOT HERE. It was never run, so putting it in the
# reproduction would generate a candidate the committed report does not contain
# and change the table. Run it deliberately when round 2 asks the question:
#   uv run --no-sync python -m scripts.beatbench.run_beatgrid_lane \
#       --fixtures ops/beatbench/round-1/fixtures-subset90.json \
#       --label beatgrid_lane_t035 --threshold 0.35 --device cpu \
#       --out ops/beatbench/round-2/candidate-beatgrid_lane_t035.json \
#       --raw-out ops/beatbench/round-2/raw-beatgrid_lane_t035.json
#
# TIMING IS MEASURED SERIALLY, ONE CANDIDATE AT A TIME. Round 0 recorded an 86x
# distortion on librosa from running candidates concurrently. Do not "speed this
# up" by backgrounding the arms.
set -euo pipefail

MODE="${1:-}"
case "$MODE" in
    reproduce|full-model-arm) shift ;;
    *)
        echo "usage: $0 {reproduce|full-model-arm} [BASE_URL]" >&2
        exit 2
        ;;
esac

BASE="${1:-http://127.0.0.1:8681}"
OUT="ops/beatbench/round-1"
SCRATCH=".tmp/beatbench-r1"
R0="ops/beatbench/round-0"

if [ "$MODE" = "reproduce" ]; then
    MODEL_FIXTURES="$OUT/fixtures-subset90.json"
    MODEL_SUFFIX=""
else
    MODEL_FIXTURES="$OUT/fixtures.json"
    MODEL_SUFFIX="-full337"
fi

mkdir -p "$OUT" "$SCRATCH/wav" "$SCRATCH/activations"

echo "[round1] mode=$MODE model arm over $MODEL_FIXTURES"

echo "[round1] 1/6 rebuild the excerpts at their recorded windows"
# REBUILD FROM THE ROUND-1 MANIFEST, NOT ROUND-0'S. Round 0 recorded no
# `wav_sha256`, so rebuilding from it gives `rebuild_one` nothing to compare
# against and every freshly decoded byte is accepted as a new baseline: the
# checksum guard exists but cannot fire (Codex P1 BLOCKING on PR #1514). The
# committed round-1 manifest DOES carry a digest for all 337 fixtures, so
# rebuilding from it makes drift on any track a named drop rather than a
# silent re-baseline. Written to a scratch path first so the comparison is
# never against a file this command has already overwritten.
uv run --no-project --script scripts/beatbench/fixtures.py \
    --base "$BASE" --rebuild-from "$OUT/fixtures.json" \
    --out "$SCRATCH/fixtures-rebuilt.json" --wav-dir "$SCRATCH/wav" --workers 6

echo "[round1] 1b/6 control: the rebuild dropped NO fixture from the committed set"
# The subset control below cannot see a drop, in either mode. In reproduce mode
# it only inspects the 90 selected tracks, so drift in any of the other 247
# passes unnoticed while the constant and librosa arms still overwrite the
# committed round with different denominators. In full-model-arm mode
# MODEL_FIXTURES IS the rebuild output, so both of its arguments are the same
# file and it compares a manifest with itself: a check that cannot fail
# (Codex P1 BLOCKING on PR #1514). `fixtures.py --rebuild-from` exits 0 having
# written a REDUCED manifest when it drops a fixture, so the count is the only
# thing that says so.
#
# Pinned to the INPUT's count rather than to the literal 337, so the control
# states an invariant that cannot go stale the next time the fixture set is
# legitimately resized.
python3 - "$OUT/fixtures.json" "$SCRATCH/fixtures-rebuilt.json" <<'REBUILD_CONTROL'
import json
import sys

before = len(json.load(open(sys.argv[1]))["fixtures"])
after = len(json.load(open(sys.argv[2]))["fixtures"])
print(f"[rebuild-control] the committed set has {before} fixtures, the rebuild produced {after}")
if after != before:
    print(
        f"[rebuild-control] FAILED: {before - after} fixture(s) dropped. This round is "
        f"NOT a reproduction of the committed round: every arm below would score against a "
        f"smaller corpus while the report still reads like the committed one. "
        f"Investigate the drops before re-running; do not proceed."
    )
    raise SystemExit(1)
print("[rebuild-control] OK: no fixture dropped, the corpus is the committed corpus")
REBUILD_CONTROL

# ONLY NOW is the committed manifest replaced. Copying before the control
# above would have made it compare the new file against itself, which is the
# very defect being fixed one level down.
cp "$SCRATCH/fixtures-rebuilt.json" "$OUT/fixtures.json"

echo "[round1] 2/6 control: the model arm's manifest is a real subset of the rebuild"
# A control that CAN fail: if the rebuild moved a window, changed a decode or
# dropped a track, the subset manifest the model arm scores would no longer
# describe the same audio as the fixture set the report divides by, and every
# figure would be comparing two different corpora while looking fine.
python3 - "$OUT/fixtures.json" "$MODEL_FIXTURES" <<'PY'
import json
import os
import sys

full = {f["stable_id"]: f for f in json.load(open(sys.argv[1]))["fixtures"]}
subset = json.load(open(sys.argv[2]))["fixtures"]
missing = [f["stable_id"] for f in subset if f["stable_id"] not in full]
changed = [
    f["stable_id"]
    for f in subset
    if f["stable_id"] in full and full[f["stable_id"]]["wav_sha256"] != f["wav_sha256"]
]
absent = [f["wav"] for f in subset if not os.path.isfile(f["wav"])]
print(f"[subset-control] {len(subset)} of {len(full)} fixtures")
if missing or changed or absent:
    print(f"[subset-control] FAILED: {len(missing)} missing, {len(changed)} changed audio, "
          f"{len(absent)} wav files absent")
    for sid in (missing + changed)[:5]:
        print(f"[subset-control]   {sid}")
    raise SystemExit(1)
print("[subset-control] OK: every fixture present with byte-identical audio")
PY

echo "[round1] 3/6 control: the local peak picker still matches beat_this exactly"
uv run --no-project --script apps/analysis_beatgrid/beat_this_runner.py \
    --manifest "$MODEL_FIXTURES" --out /dev/null --device cpu --verify-postprocessor

echo "[round1] 4/6 candidates, serial"
uv run --no-sync python -m scripts.beatbench.run_beatgrid_lane \
    --fixtures "$MODEL_FIXTURES" --label beatgrid_lane_t050 \
    --out "$OUT/candidate-beatgrid_lane_t050${MODEL_SUFFIX}.json" \
    --raw-out "$OUT/raw-beatgrid_lane_t050${MODEL_SUFFIX}.json" \
    --device cpu --threshold 0.5 --activations-dir "$SCRATCH/activations"

uv run --no-project --script scripts/beatbench/run_constant_bpm.py \
    --fixtures "$OUT/fixtures.json" --out "$OUT/candidate-constant_128.json" --workers 1

uv run --no-project --script scripts/beatbench/run_librosa.py \
    --fixtures "$OUT/fixtures.json" --out "$OUT/candidate-librosa.json" --workers 1

echo "[round1] 5/6 score with the versioned scorer"
# NO --serial. Passing each just-generated full-pass artifact back as its own
# serial calibration republished `serial_realtime_factor` and the corpus
# estimate whatever the host load was, which is how the known-invalid 21.8x and
# the 30.6h estimate reached the committed report from a host at load average
# 554 (Codex P1 BLOCKING on PR #1514). A parallel full pass is not a serial
# calibration and must never be presented as one. To publish runtime, run the
# candidates one at a time on a QUIET host, confirm and record the load average
# in the round entry in specs/beat-mapping-bench.md, and pass those artifacts
# to --serial explicitly. Absent that, the report prints n/a, which is what
# "not measured" should look like.
# --fixtures is always the FULL manifest: the scorer divides each candidate by
# the rows it actually produced, so the 90-track arm reports n=60/30 and the
# 337-track arms report n=200/137 in the same table, each against its own
# denominator rather than a shared one.
uv run --no-sync python -m scripts.beatbench.report \
    --round 1 --fixtures "$OUT/fixtures.json" \
    --full "$OUT/candidate-beatgrid_lane_t050${MODEL_SUFFIX}.json" \
        "$OUT/candidate-constant_128.json" "$OUT/candidate-librosa.json" \
    --out-md "$OUT/report${MODEL_SUFFIX}.md" --out-json "$OUT/results${MODEL_SUFFIX}.json"

echo "[round1] 6/6 control: cross-check CMLt/AMLt against mir_eval on real beat times"
# TWO SUBJECTS, AND THE SECOND IS CURRENTLY RED. Run against the constant-128
# arm this passes on all 337; run it against a REAL tracker and it does not.
# Measured Tue 8 Sep 2026 on the committed artifacts: librosa 4 of 337 disagree,
# the model arm 2 of 85, worst CMLt difference 1.37e-02, always one correct beat
# short of mir_eval. That is a live defect in this repo's continuity code, or in
# how the two implementations break a tie, and it is NOT yet diagnosed
# (.planning/debt/1514.md).
#
# The round-1 PR body quoted "all 337 tracks agree" for this control. That run
# was the constant-128 arm alone, which is a rigid grid at one tempo from t=0:
# it cannot exercise irregular spacing, so it is a control that could not fail
# for the reason under test. The lane arm was never put through it. Which is
# why BOTH are run here, the regular one first as the instrument check and the
# real one second as the subject.
#
# `set -e` means this script exits nonzero at this step today. That is the TRUE
# state of the round, not broken plumbing, and the fix is to diagnose the
# disagreement rather than to soften this step.
uv run --no-project --script scripts/beatbench/verify_continuity.py \
    --fixtures "$OUT/fixtures.json" --candidate "$OUT/candidate-constant_128.json"
uv run --no-project --script scripts/beatbench/verify_continuity.py \
    --fixtures "$MODEL_FIXTURES" \
    --candidate "$OUT/candidate-beatgrid_lane_t050${MODEL_SUFFIX}.json"

echo "[OK] round 1 ($MODE) -> $OUT/report${MODEL_SUFFIX}.md"
