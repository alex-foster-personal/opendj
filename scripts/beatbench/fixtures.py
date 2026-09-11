#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Build the stratified fixture set, and decode one identical excerpt per track.

EVERY CANDIDATE MUST READ THE SAME BYTES. If each analyzer opened the source
file itself, the comparison would silently include five different decoders,
five different resamplers and five different channel-downmixes. Any of those
can move a beat by a millisecond or two, which is the same order as the thing
being measured. So this decodes ONCE to a plain 44.1 kHz mono WAV and every
runner reads that file. Decode differences stop being a variable.

AUDIO COMES THROUGH THE DAEMON, NEVER OFF A DB PATH. rekordbox's stored
FolderPath is frequently stale; the healing that maps it onto a file that
actually exists lives in the daemon. ffmpeg reads the daemon URL directly and
range-seeks into it, so a 45 second excerpt costs a fraction of a second and
never pulls a whole 10 MB file.

THE NON-SILENCE ASSERT IS NOT PARANOIA. A path can heal onto a file that opens,
decodes, and contains nothing - a truncated download, a DRM stub, a gap in a
recording. That excerpt would score as a total analyzer failure and quietly
libel whichever candidate drew it. Measuring peak level and dropping anything
that fails is what keeps a decode problem from being reported as a beat
tracking problem.

WINDOW PLACEMENT. The excerpt starts at 25 percent of the track, which skips
the intro, where a grid is least constrained by audible rhythm. The scoring
window is then the excerpt inset by a guard band at each end, because beat
trackers are unreliable in their first and last moments and that is an artifact
of excerpting rather than a property of the analyzer.

SAMPLING IS DELIBERATELY NOT PROPORTIONAL, AND THE REPORT MUST NOT BLEND IT.
Every dynamic-grid track is taken because fixed-BPM analysis can conceal
errors on that population. The fixed-grid sample is stratified across BPM
bands so a densely populated band cannot drown out the rest. This is
deliberately unrepresentative: the manifest records population counts, and
reports must weight by them rather than quote a single blended headline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# ----- Configuration ------------------------------------------------------

EXCERPT_S = 45.0          # long enough for ~90 beats at 120 BPM
GUARD_S = 2.0             # trimmed off each end before scoring
START_FRACTION = 0.25     # skip the intro
SAMPLE_RATE = 44100
MIN_DURATION_S = 60.0     # below this an excerpt plus guard bands does not fit

# A decoded excerpt whose loudest sample is below this is treated as no audio.
MIN_PEAK_DBFS = -40.0

BPM_BAND_WIDTH = 10
MIN_PER_BAND = 5

_VOL_RE = re.compile(r"(mean|max)_volume:\s*(-?[\d.]+) dB")


# ----- Selection ----------------------------------------------------------


def _band(bpm: float) -> int:
    return int(bpm // BPM_BAND_WIDTH) * BPM_BAND_WIDTH


def select_fixed(tracks: list[dict[str, Any]], want: int, rng: random.Random) -> list[dict]:
    """Stratify the fixed-grid pool across BPM bands.

    Proportional-with-a-floor: each band gets its share of the target, but any
    band holding at least MIN_PER_BAND tracks is guaranteed that many, so the
    thin tails of the library stay visible instead of rounding to zero.
    """
    bands: dict[int, list[dict]] = defaultdict(list)
    for t in tracks:
        bands[_band(t["rb_bpm"])].append(t)

    total = len(tracks)
    picked: list[dict] = []
    for _band_bpm, members in sorted(bands.items()):
        share = round(want * len(members) / total)
        floor = MIN_PER_BAND if len(members) >= MIN_PER_BAND else len(members)
        take = min(len(members), max(share, floor))
        picked.extend(rng.sample(members, take))
    rng.shuffle(picked)
    return picked[:want] if len(picked) > want else picked


# ----- Decode -------------------------------------------------------------


def decode_excerpt(
    base: str, track: dict[str, Any], out_path: str, start_s: float | None = None
) -> dict[str, Any]:
    """Range-seek the daemon audio stream and write one mono WAV excerpt.

    Returns the measured levels so the caller can enforce the non-silence rule.
    Any ffmpeg failure is returned as a reason string rather than raised, so one
    bad file cannot abort a 300 track build.

    `start_s` is supplied only by the rebuild path, which must reproduce a
    recorded window EXACTLY rather than recompute it: recomputing would silently
    move the excerpt if a duration were ever corrected, and every round-over-
    round delta would then be unattributable.
    """
    duration_s = (track["duration_ms"] or 0) / 1000.0
    if start_s is not None:
        start = float(start_s)
    else:
        start = round(duration_s * START_FRACTION, 3)
        if start + EXCERPT_S > duration_s:
            start = round(max(0.0, (duration_s - EXCERPT_S) / 2.0), 3)

    url = f"{base.rstrip('/')}/api/v1/tracks/{track['stable_id']}/audio"
    cmd = [
        "ffmpeg", "-nostdin", "-v", "info", "-ss", str(start), "-t", str(EXCERPT_S),
        "-i", url, "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-af", "volumedetect", "-f", "wav", "-y", out_path,
    ]
    # check=False: a non-zero ffmpeg exit is a per-track drop reason this
    # function returns, not an exception that should abort a 300 track build.
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0:
        return {
            "ok": False,
            "reason": f"ffmpeg exit {proc.returncode}: {proc.stderr.strip()[-180:]}",
        }

    levels = {m.group(1): float(m.group(2)) for m in _VOL_RE.finditer(proc.stderr)}
    if "max" not in levels:
        return {"ok": False, "reason": "ffmpeg reported no volume statistics"}
    if levels["max"] < MIN_PEAK_DBFS:
        return {"ok": False, "reason": f"silent excerpt: peak {levels['max']:.1f} dBFS"}
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
        return {"ok": False, "reason": "ffmpeg wrote no usable wav"}

    return {
        "ok": True,
        "window_start_s": start,
        "window_end_s": round(start + EXCERPT_S, 3),
        "mean_volume_dbfs": levels.get("mean"),
        "max_volume_dbfs": levels["max"],
        "bytes": os.path.getsize(out_path),
    }


def fetch_reference_beats(base: str, stable_id: str, start: float, end: float) -> list[list[float]]:
    """The rekordbox grid inside the excerpt, as compact [n, t, bpm] triples."""
    url = f"{base.rstrip('/')}/api/v1/tracks/{stable_id}/anlz?points=100"
    with urllib.request.urlopen(url, timeout=90) as resp:
        anlz = json.loads(resp.read().decode("utf-8"))
    beats = (anlz.get("beatgrid") or {}).get("beats") or []
    return [
        [int(b["n"]), round(float(b["t"]), 4), round(float(b["bpm"]), 2)]
        for b in beats
        if start <= float(b["t"]) < end
    ]


# ----- Build --------------------------------------------------------------


def build_one(base: str, track: dict[str, Any], wav_dir: str) -> dict[str, Any]:
    out_path = os.path.join(wav_dir, f"{track['stable_id']}.wav")
    decoded = decode_excerpt(base, track, out_path)
    if not decoded["ok"]:
        if os.path.exists(out_path):
            os.remove(out_path)
        return {"ok": False, "stable_id": track["stable_id"], "reason": decoded["reason"]}

    beats = fetch_reference_beats(
        base, track["stable_id"], decoded["window_start_s"], decoded["window_end_s"]
    )
    if len(beats) < 8:
        os.remove(out_path)
        return {
            "ok": False,
            "stable_id": track["stable_id"],
            "reason": f"only {len(beats)} rekordbox beats inside the excerpt window",
        }

    return {
        "ok": True,
        "stable_id": track["stable_id"],
        "title": track["title"],
        "rb_bpm": track["rb_bpm"],
        "duration_ms": track["duration_ms"],
        "is_dynamic": len(track["distinct_bpms"]) > 1,
        "grid_beat_count": track["beat_count"],
        "grid_bpm_span": round(max(track["distinct_bpms"]) - min(track["distinct_bpms"]), 2),
        "window_start_s": decoded["window_start_s"],
        "window_end_s": decoded["window_end_s"],
        "score_start_s": round(decoded["window_start_s"] + GUARD_S, 3),
        "score_end_s": round(decoded["window_end_s"] - GUARD_S, 3),
        "wav": out_path,
        "mean_volume_dbfs": decoded["mean_volume_dbfs"],
        "max_volume_dbfs": decoded["max_volume_dbfs"],
        "ref_beats": beats,
    }



# ----- Rebuild ------------------------------------------------------------


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def rebuild_one(base: str, fixture: dict[str, Any], wav_dir: str) -> dict[str, Any]:
    """Re-decode ONE fixture's excerpt at its recorded window, and re-verify it.

    THIS IS NOT A FRESH BUILD AND MUST NOT BECOME ONE. The excerpt WAVs are
    gitignored scratch, roughly 1.2 GB, so a later round has to regenerate them
    while keeping the fixture SET identical -- same tracks, same windows, same
    scoring bands -- or nothing measured against them is comparable with round
    0. So the selection, the seed and the sampling never run here; only the
    decode does.

    Two checks make a silent divergence loud rather than plausible:

      - The rekordbox grid is re-fetched and compared against the manifest's
        stored `ref_beats`. The library is live: a re-analysis, a relink or a
        re-import between rounds would move the ground truth underneath the
        comparison, and a rebuilt fixture whose reference changed is NOT the
        round-0 fixture whatever its filename says. A mismatch is reported per
        fixture rather than raised, so the round can proceed on the fixtures
        that did survive with the drifted ones named and excluded.
      - The decoded bytes are hashed AND COMPARED. Round 0 recorded no
        checksums, so the first rebuild can only establish the baseline; from
        round 1 on a recorded digest is compared and a mismatch DROPS the
        fixture by name. A rebuild that overwrote the recorded digest instead
        would adopt the drift as the new baseline, which is a check that cannot
        fail (Codex P1 on PR #1514, and correct).
    """
    out_path = os.path.join(wav_dir, f"{fixture['stable_id']}.wav")
    track = {
        "stable_id": fixture["stable_id"],
        "duration_ms": fixture["duration_ms"],
    }
    decoded = decode_excerpt(base, track, out_path, start_s=fixture["window_start_s"])
    if not decoded["ok"]:
        if os.path.exists(out_path):
            os.remove(out_path)
        return {"ok": False, "stable_id": fixture["stable_id"], "reason": decoded["reason"]}

    if abs(decoded["window_start_s"] - fixture["window_start_s"]) > 1e-6:
        return {
            "ok": False,
            "stable_id": fixture["stable_id"],
            "reason": (
                f"window moved: recorded {fixture['window_start_s']} "
                f"rebuilt {decoded['window_start_s']}"
            ),
        }

    fresh_beats = fetch_reference_beats(
        base, fixture["stable_id"], fixture["window_start_s"], fixture["window_end_s"]
    )
    if fresh_beats != fixture["ref_beats"]:
        os.remove(out_path)
        return {
            "ok": False,
            "stable_id": fixture["stable_id"],
            "reason": (
                f"rekordbox grid changed since the manifest was built: "
                f"{len(fixture['ref_beats'])} stored beats, {len(fresh_beats)} now"
            ),
        }

    digest = _sha256_file(out_path)
    recorded = fixture.get("wav_sha256")
    if recorded is not None and recorded != digest:
        # The whole point of recording the digest is that a decoder, ffmpeg or
        # source-audio change becomes VISIBLE on the next rebuild. Overwriting
        # it here would silently adopt the drift as the new baseline and let a
        # decode change be misattributed to a candidate, which is the exact
        # failure the docstring above promises this catches.
        os.remove(out_path)
        return {
            "ok": False,
            "stable_id": fixture["stable_id"],
            "reason": (
                f"excerpt bytes changed since the manifest was built: recorded "
                f"{recorded[:16]} rebuilt {digest[:16]} (decoder, ffmpeg or source audio)"
            ),
        }

    rebuilt = dict(fixture)
    rebuilt["wav"] = out_path
    rebuilt["wav_sha256"] = digest
    rebuilt["wav_bytes"] = os.path.getsize(out_path)
    rebuilt["rebuilt_max_volume_dbfs"] = decoded["max_volume_dbfs"]
    return {"ok": True, **rebuilt}


def rebuild(base: str, manifest_path: str, out_path: str, wav_dir: str, workers: int) -> int:
    started = time.time()
    os.makedirs(wav_dir, exist_ok=True)
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    fixtures = manifest["fixtures"]
    print(f"[rebuild] {len(fixtures)} fixtures from {manifest_path}", flush=True)

    rebuilt: list[dict] = []
    dropped: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, res in enumerate(
            pool.map(lambda f: rebuild_one(base, f, wav_dir), fixtures), 1
        ):
            (rebuilt if res["ok"] else dropped).append(res)
            if i % 50 == 0:
                print(f"[rebuild]   {i}/{len(fixtures)}", flush=True)

    for row in rebuilt:
        row.pop("ok", None)
    n_dyn = sum(1 for r in rebuilt if r["is_dynamic"])

    payload = dict(manifest)
    payload["fixtures"] = rebuilt
    payload["dropped"] = dropped
    payload["rebuilt_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["rebuilt_from"] = manifest_path
    payload["rebuild_elapsed_s"] = round(time.time() - started, 1)
    payload["selection"] = dict(manifest["selection"])
    payload["selection"].update({
        "rebuilt_total": len(rebuilt),
        "rebuilt_fixed": len(rebuilt) - n_dyn,
        "rebuilt_dynamic": n_dyn,
        "rebuild_dropped": len(dropped),
    })
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    print(
        f"[rebuild] done in {payload['rebuild_elapsed_s']}s: "
        f"{len(rebuilt)}/{len(fixtures)} rebuilt "
        f"({payload['selection']['rebuilt_fixed']} fixed, "
        f"{payload['selection']['rebuilt_dynamic']} dynamic) -> {out_path}",
        flush=True,
    )
    for row in dropped:
        print(f"[rebuild]   DROPPED {row['stable_id']}: {row['reason']}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8685")
    ap.add_argument("--survey", help="survey.json; required unless --rebuild-from is given")
    ap.add_argument(
        "--rebuild-from",
        help="an existing fixtures.json: re-decode ITS excerpts, never reselect",
    )
    ap.add_argument("--out", required=True)
    ap.add_argument("--wav-dir", required=True, help="excerpt scratch, never committed")
    ap.add_argument("--fixed", type=int, default=200)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--seed", type=int, default=20260819)
    args = ap.parse_args()

    if args.rebuild_from:
        if args.survey:
            raise SystemExit(
                "[fixtures] --survey and --rebuild-from are mutually exclusive: a rebuild "
                "must not reselect, and a fresh build has nothing to rebuild from"
            )
        return rebuild(args.base, args.rebuild_from, args.out, args.wav_dir, args.workers)
    if not args.survey:
        raise SystemExit("[fixtures] pass --survey to build, or --rebuild-from to re-decode")

    started = time.time()
    os.makedirs(args.wav_dir, exist_ok=True)
    with open(args.survey, encoding="utf-8") as fh:
        survey = json.load(fh)

    eligible = [
        t for t in survey["tracks"]
        if t["beat_count"] > 0
        and not t["error"]
        and t["rb_bpm"]
        and (t["duration_ms"] or 0) / 1000.0 >= MIN_DURATION_S
    ]
    dynamic = [t for t in eligible if len(t["distinct_bpms"]) > 1]
    fixed = [t for t in eligible if len(t["distinct_bpms"]) == 1]
    print(f"[fixtures] eligible: {len(fixed)} fixed, {len(dynamic)} dynamic", flush=True)

    rng = random.Random(args.seed)
    chosen = select_fixed(fixed, args.fixed, rng) + dynamic
    print(f"[fixtures] building {len(chosen)} excerpts into {args.wav_dir}", flush=True)

    built: list[dict] = []
    dropped: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        built_iter = pool.map(lambda t: build_one(args.base, t, args.wav_dir), chosen)
        for i, res in enumerate(built_iter, 1):
            (built if res["ok"] else dropped).append(res)
            if i % 50 == 0:
                print(f"[fixtures]   {i}/{len(chosen)}", flush=True)

    for row in built:
        row.pop("ok", None)
    n_dyn = sum(1 for r in built if r["is_dynamic"])

    payload = {
        "schema": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(time.time() - started, 1),
        "base": args.base,
        "seed": args.seed,
        "excerpt": {
            "length_s": EXCERPT_S,
            "guard_s": GUARD_S,
            "start_fraction": START_FRACTION,
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "min_peak_dbfs": MIN_PEAK_DBFS,
            "min_duration_s": MIN_DURATION_S,
        },
        "denominator": survey["denominator"],
        "population": {
            "eligible_fixed": len(fixed),
            "eligible_dynamic": len(dynamic),
            "note": (
                "Dynamic grids are fully enumerated while fixed grids are sampled, so the "
                "fixture set is NOT proportional to the library. Weight by these counts "
                "before quoting any library-wide figure."
            ),
        },
        "selection": {
            "fixed_requested": args.fixed,
            "built_total": len(built),
            "built_fixed": len(built) - n_dyn,
            "built_dynamic": n_dyn,
            "dropped": len(dropped),
        },
        "dropped": dropped,
        "fixtures": built,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    print(f"[fixtures] done in {payload['elapsed_s']}s -> {args.out}", flush=True)
    for k, v in payload["selection"].items():
        print(f"[fixtures]   {k:18s} {v}", flush=True)
    if dropped:
        reasons: dict[str, int] = defaultdict(int)
        for row in dropped:
            reasons[row["reason"].split(":")[0][:60]] += 1
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"[fixtures]   dropped {count:4d}  {reason}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
