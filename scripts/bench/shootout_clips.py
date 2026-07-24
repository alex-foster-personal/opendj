# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Cut the shootout down to a listenable set and emit a rater manifest.

Every metric in this benchmark is a proxy. SI-SDR, SIR, SAR and LSD all measure
something real, but none of them is "does this sound like a usable vocal stem",
and the whole point of the exercise is a production decision. So the top models
get encoded to clips a human can actually play, alongside the TRUE vocal stem as
the reference and the mixture for context.

AAC 256k m4a rather than FLAC: the rating page loads every clip in a browser and
FLAC at these lengths makes the page unusable. 256k AAC is transparent enough
that it cannot flip a judgement between two separators, which is the only
comparison being made here.

Which models: the top N by median SI-SDR among TEST-SET-CLEAN models only. The
leaked pair (mdx_extra, mdx_extra_q) is excluded on purpose. They top the raw
metric, but they trained on these exact songs, so a listening test on MUSDB
material would be judging memorisation and would push a listener toward a model
that cannot repeat the trick on a real library. They stay in the tables as
evidence and out of anything anyone is asked to choose from.

Which tracks: the anchor track (the one the original single-track surprise came
from, so the old claim can be re-checked by ear) plus the track where the clean
models disagree most, since that is where listening is most informative.

Requirements (mini-PRD):
  ✔︎ ✅ every m4a is duration-probed after encoding and must match its source
    FLAC; a previous agent in this repo shipped truncated audio.
    [if] an encoded clip is short of its source [then ⛔️] RuntimeError
  ✔︎ ✅ the manifest is the same shape vocal_quality_rater.html already consumes
    (track, reference, clips[], mixture_file, window) with file paths relative
    to the manifest's own directory.
    [if] a referenced file is absent from the output dir [then ⛔️] RuntimeError
  ✔︎ ✅ refuses to run when a source FLAC is missing rather than emitting a
    manifest with holes in it.

Run:
  uv run scripts/bench/shootout_clips.py --source-dir .tmp/bench/shootout
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import shootout_spec

_BENCH_DIR: Path = Path(__file__).resolve().parent
_IN_JSON: Path = _BENCH_DIR / "model_shootout.json"
_OUT_DIR: Path = _BENCH_DIR / "clips-shootout"
_OUT_MANIFEST: Path = _OUT_DIR / "ladder.json"

TOP_N_MODELS: int = 3
# The track the original one-clip surprise came from. Carried so a listener can
# check the claim that started this on the exact material that produced it.
ANCHOR_SLUG: str = "al-james-schoolboy-facination"
AAC_BITRATE: str = "256k"
# m4a container duration can legitimately differ from the FLAC by one AAC frame
# (1024 samples, ~23ms at 44.1 kHz) plus encoder priming.
_DURATION_TOL_S: float = 0.15


def _require_tool(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise RuntimeError(f"required tool not on PATH: {tool}")
    return path


def _duration_s(path: Path) -> float:
    out = subprocess.run(
        [
            _require_tool("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not out:
        raise RuntimeError(f"ffprobe returned no duration for {path}")
    return float(out)


def _encode_m4a(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"source audio missing: {source}")
    subprocess.run(
        [
            _require_tool("ffmpeg"),
            "-y",
            "-i",
            str(source),
            "-c:a",
            "aac",
            "-b:a",
            AAC_BITRATE,
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    source_s = _duration_s(source)
    out_s = _duration_s(destination)
    if abs(source_s - out_s) > _DURATION_TOL_S:
        raise RuntimeError(
            f"{destination} encoded to {out_s:.3f}s from a {source_s:.3f}s source "
            "-- truncated, refusing to publish it"
        )


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _top_models(manifest: dict[str, Any]) -> list[str]:
    """Top N by median SI-SDR among test-set-clean models. Contaminated models
    are never offered for listening; see the module docstring."""
    medians: dict[str, float] = {}
    for name in manifest["models"]:
        if not shootout_spec.is_test_set_clean(name):
            continue
        scores = [
            track["results"][name]["si_sdr"]
            for track in manifest["tracks"]
            if "si_sdr" in track["results"].get(name, {})
        ]
        if scores:
            medians[name] = _median(scores)
    if len(medians) < TOP_N_MODELS:
        raise RuntimeError(
            f"only {len(medians)} test-set-clean models have scores, need "
            f"{TOP_N_MODELS} for the listening set"
        )
    return sorted(medians, key=lambda k: medians[k], reverse=True)[:TOP_N_MODELS]


def _chosen_tracks(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    by_slug = {track["slug"]: track for track in manifest["tracks"]}
    if ANCHOR_SLUG not in by_slug:
        raise RuntimeError(f"anchor track {ANCHOR_SLUG} absent from {_IN_JSON}")
    chosen = [by_slug[ANCHOR_SLUG]]

    def clean_spread(track: dict[str, Any]) -> float:
        scores = [
            result["si_sdr"]
            for name, result in track["results"].items()
            if "si_sdr" in result and shootout_spec.is_test_set_clean(name)
        ]
        return max(scores) - min(scores) if scores else 0.0

    rest = [t for t in manifest["tracks"] if t["slug"] != ANCHOR_SLUG]
    chosen.append(max(rest, key=clean_spread))
    return chosen


def _track_block(track: dict[str, Any], models: list[str], source_dir: Path) -> dict[str, Any]:
    slug = track["slug"]
    _encode_m4a(source_dir / f"{slug}-truth-vocals.flac", _OUT_DIR / f"{slug}-truth-vocals.m4a")
    _encode_m4a(source_dir / f"{slug}-mixture.flac", _OUT_DIR / f"{slug}-mixture.m4a")

    scored = [(name, track["results"][name]["si_sdr"]) for name in models]
    ranks = {
        name: position + 1
        for position, (name, _) in enumerate(sorted(scored, key=lambda kv: kv[1]))
    }
    clips = []
    for name in models:
        _encode_m4a(source_dir / f"{slug}-{name}.flac", _OUT_DIR / f"{slug}-{name}.m4a")
        _encode_m4a(
            source_dir / f"{slug}-{name}.inst.flac", _OUT_DIR / f"{slug}-{name}.inst.m4a"
        )
        result = track["results"][name]
        training, clean = shootout_spec.provenance(name)
        clips.append(
            {
                "id": name,
                "file": f"{slug}-{name}.m4a",
                "inst_file": f"{slug}-{name}.inst.m4a",
                "machine_score": ranks[name],
                "params": f"{name} overlap={shootout_spec.OVERLAP}",
                "separate_s": result["separate_s"],
                "si_sdr": result["si_sdr"],
                "si_sdr_pseudo": None,
                "sir_db": result["sir_db"],
                "sar_db": result["sar_db"],
                "training_data": training,
                "test_set_clean": clean,
            }
        )
    return {
        "track": f"{track['track']} (MUSDB18-HQ)",
        "slug": slug,
        "genre": track["genre"],
        "reference": f"{slug}-truth-vocals.m4a",
        "mixture_file": f"{slug}-mixture.m4a",
        "window": track["window"],
        "clips": clips,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    args = parser.parse_args()
    if not _IN_JSON.is_file():
        raise RuntimeError(f"missing input: {_IN_JSON}")
    manifest = json.loads(_IN_JSON.read_text(encoding="utf-8"))

    models = _top_models(manifest)
    tracks = _chosen_tracks(manifest)
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"top {TOP_N_MODELS} by median SI-SDR: {', '.join(models)}")
    print(f"tracks: {', '.join(t['track'] for t in tracks)}")

    blocks = [_track_block(track, models, args.source_dir) for track in tracks]
    out = dict(blocks[0])
    out["reference_kind"] = "true-stem"
    out["dataset"] = manifest["dataset"]
    out["tracks"] = blocks
    _OUT_MANIFEST.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    for block in blocks:
        for relative in [block["reference"], block["mixture_file"]] + [
            path for clip in block["clips"] for path in (clip["file"], clip["inst_file"])
        ]:
            if not (_OUT_DIR / relative).is_file():
                raise RuntimeError(f"manifest references a missing file: {relative}")
    count = sum(2 + 2 * len(b["clips"]) for b in blocks)
    print(f"[OK] wrote {count} m4a clips and {_OUT_MANIFEST}")


if __name__ == "__main__":
    main()
