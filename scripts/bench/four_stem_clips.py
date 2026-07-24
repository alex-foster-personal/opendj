# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Encode the four-stem ladder to m4a and emit ONE RATER MANIFEST PER STEM.

vocal_quality_rater.html carries a single `reference` per manifest, so a page
that mixed drums and bass clips would have to point them at the same reference
stem, which is meaningless. Four manifests, one per stem, is the shape the page
can actually express: each has the TRUE stem for that source as its reference,
and its four graded clips are that source from each config.

What is a graded clip and what is a reference, restated because the last round
got this wrong:
  - graded clips: real separator outputs only, four per stem.
  - references: the TRUE stem (top of the page) and the raw mixture. The mixture
    is never an arm. Its per-stem SI-SDR lives in the index as the do-nothing
    floor, which is context, not a grade.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 every m4a duration-probed against its wav source before it is published
    [if] an encoded clip is short of its source [then ⛔️] RuntimeError
  ✔︎ ✅ 🎯 four per-stem manifests in the shape the rater validates
    [if] a manifest references a file not on disk [then ⛔️] RuntimeError
    [if] two clips in one manifest share a machine_score [then ⛔️] RuntimeError
  ✔︎ ✅ 🎯 an index manifest naming the four, with the config x stem matrix

Run:
  uv run scripts/bench/four_stem_clips.py --source-dir .tmp/bench-4stem

-Claude
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

_BENCH_DIR: Path = Path(__file__).resolve().parent
_IN_JSON: Path = _BENCH_DIR / "four_stem_ladder.json"
_OUT_DIR: Path = _BENCH_DIR / "clips-4stem"
_CLIPS_REL: str = "clips-4stem"
_INDEX: Path = _BENCH_DIR / "ladder_4stem.json"

AAC_BITRATE: str = "256k"
# m4a container duration can legitimately differ from the wav by one AAC frame
# (1024 samples, ~23ms at 44.1 kHz) plus encoder priming.
_DURATION_TOL_S: float = 0.15


def _require_tool(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise RuntimeError(f"required tool not on PATH: {tool}")
    return path


def _duration_s(path: Path) -> float:
    out = subprocess.run(
        [_require_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    if not out:
        raise RuntimeError(f"ffprobe returned no duration for {path}")
    return float(out)


def _encode_m4a(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"source audio missing: {source}")
    subprocess.run(
        [_require_tool("ffmpeg"), "-y", "-i", str(source), "-c:a", "aac",
         "-b:a", AAC_BITRATE, str(destination)],
        capture_output=True, text=True, check=True)
    source_s = _duration_s(source)
    out_s = _duration_s(destination)
    if abs(source_s - out_s) > _DURATION_TOL_S:
        raise RuntimeError(
            f"{destination} encoded to {out_s:.3f}s from a {source_s:.3f}s source "
            "-- truncated, refusing to publish it")


def _stem_manifest(ladder: dict[str, Any], stem: str, slug: str) -> dict[str, Any]:
    arms = ladder["arms"]
    ordered = sorted(arms, key=lambda a: a["si_sdr"][stem])
    score_of = {arm["id"]: rank + 1 for rank, arm in enumerate(ordered)}

    clips = []
    for arm in arms:
        clips.append({
            "id": arm["id"],
            "file": f"{_CLIPS_REL}/{slug}-{arm['id']}-{stem}.m4a",
            "inst_file": None,
            "params": f"{arm['params']} [{arm['s_per_stem_minute']} s per stem-minute "
                      "on Apple M3 CPU, all four stems from one pass]",
            "separate_s": arm["infer_s"],
            "si_sdr": arm["si_sdr"][stem],
            "s_per_stem_minute": arm["s_per_stem_minute"],
            "machine_score": score_of[arm["id"]],
        })
    scores = [c["machine_score"] for c in clips]
    if len(set(scores)) != len(scores):
        raise RuntimeError(f"{stem} manifest has duplicate machine_score values")

    return {
        "track": f"{ladder['track']} -- {stem.upper()} stem",
        "stem": stem,
        # Blind by default. The last two rounds were rated with the params string
        # and the machine score on screen, and the arms whose labels said they
        # were good came back tied at the top. The rater can still turn it off.
        "blind_default": True,
        "genre": ladder["genre"],
        "dataset": ladder["dataset"],
        "reference": f"{_CLIPS_REL}/{slug}-truth-{stem}.m4a",
        "reference_kind": "TRUE MUSDB18-HQ stem",
        "mixture_file": f"{_CLIPS_REL}/{slug}-mixture.m4a",
        "mixture_note": "the raw mixture, REFERENCE ONLY - it is not an arm and is "
                        "not graded. Its SI-SDR against this true stem is the "
                        "do-nothing floor.",
        "mixture_floor_si_sdr": ladder["mixture_floor_si_sdr"][stem],
        "window": ladder["window"],
        "hardware": ladder["hardware"],
        "clips": clips,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source-dir", required=True, type=Path)
    ap.add_argument("--slug", default="timboz-pony")
    args = ap.parse_args()

    if not _IN_JSON.is_file():
        raise RuntimeError(f"missing input: {_IN_JSON}")
    ladder = json.loads(_IN_JSON.read_text(encoding="utf-8"))
    stems: list[str] = ladder["stems"]
    slug: str = args.slug
    _OUT_DIR.mkdir(parents=True, exist_ok=True)

    _encode_m4a(args.source_dir / "mixture.wav", _OUT_DIR / f"{slug}-mixture.m4a")
    for stem in stems:
        _encode_m4a(args.source_dir / f"truth-{stem}.wav",
                    _OUT_DIR / f"{slug}-truth-{stem}.m4a")
    for arm in ladder["arms"]:
        for stem in stems:
            _encode_m4a(args.source_dir / f"{arm['id']}-{stem}.wav",
                        _OUT_DIR / f"{slug}-{arm['id']}-{stem}.m4a")

    manifests: list[dict[str, str]] = []
    for stem in stems:
        manifest = _stem_manifest(ladder, stem, slug)
        for relative in [manifest["reference"], manifest["mixture_file"]] + [
                c["file"] for c in manifest["clips"]]:
            if not (_BENCH_DIR / relative).is_file():
                raise RuntimeError(f"manifest references a missing file: {relative}")
        path = _BENCH_DIR / f"ladder_4stem_{stem}.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        manifests.append({"stem": stem, "manifest": path.name,
                          "open": f"vocal_quality_rater.html?manifest={path.name}"})

    matrix = {arm["id"]: arm["si_sdr"] for arm in ladder["arms"]}
    _INDEX.write_text(json.dumps({
        "track": ladder["track"],
        "genre": ladder["genre"],
        "dataset": ladder["dataset"],
        "window": ladder["window"],
        "hardware": ladder["hardware"],
        "note": "INDEX, not a rater manifest. vocal_quality_rater.html carries one "
                "reference per manifest, so each stem is rated from its own file "
                "below, each pointed at its own TRUE stem.",
        "manifests": manifests,
        "references": {
            "true_stems": [f"{_CLIPS_REL}/{slug}-truth-{s}.m4a" for s in stems],
            "mixture": f"{_CLIPS_REL}/{slug}-mixture.m4a",
        },
        "si_sdr_matrix": matrix,
        "mixture_floor_si_sdr": ladder["mixture_floor_si_sdr"],
        "cost": {arm["id"]: arm["s_per_stem_minute"] for arm in ladder["arms"]},
    }, indent=2) + "\n", encoding="utf-8")

    total = 1 + len(stems) + len(ladder["arms"]) * len(stems)
    print(f"[OK] wrote {total} m4a clips, {len(stems)} per-stem manifests and {_INDEX}")


if __name__ == "__main__":
    main()
