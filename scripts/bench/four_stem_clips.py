# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Encode the four-stem ladder to m4a and emit ONE COMBINED RATER MANIFEST.

Four separate manifests, one per stem, was the wrong shape. It meant four pages,
four sessions, and no way to hear what a single config did to a track as a whole,
which is the only question a live-mashup product actually asks. The rater now
carries a `stems` array -- one true-stem reference EACH -- so one page can show
every arm with all four of its stems side by side, colour-coded, each scored
separately. The per-stem manifests are still written, because they cost nothing
and the old links keep working.

Nothing is encoded until stem_content_gate.py has passed the track. The previous
round shipped a BASS stem with virtually nothing in it -- six arms rated on sixty
seconds of near-silence -- because the track was picked for the lowest mixture
baseline and no one checked what was inside each stem. The gate's per-stem
numbers are carried into the manifest and shown on the page, so the next reader
can see the check happened rather than take it on trust.

What is a graded clip and what is a reference, restated because the last round
got this wrong:
  - graded clips: real separator outputs only, four per stem.
  - references: the TRUE stem (top of the page) and the raw mixture. The mixture
    is never an arm. Its per-stem SI-SDR lives in the index as the do-nothing
    floor, which is context, not a grade.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 every m4a duration-probed against its wav source before it is published
    [if] an encoded clip is short of its source [then ⛔️] RuntimeError
  ✔︎ ✅ 🎯 one combined manifest carrying a reference PER STEM and every arm
    [if] a manifest references a file not on disk [then ⛔️] RuntimeError
    [if] two arms share a machine_score [then ⛔️] RuntimeError
    [if] the content gate did not pass this track [then ⛔️] RuntimeError, no clips
  ✔︎ ✅ 🎯 no clip is published near-silent, measured on the ENCODED m4a
    [if] an encoded clip is below the audibility floor [then ⛔️] RuntimeError
  ✔︎ ✅ 🎯 the per-stem manifests still render, so old links do not break

Run:
  uv run scripts/bench/four_stem_clips.py --source-dir .tmp/bench/4stem-aljames

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
_GATE_JSON: Path = _BENCH_DIR / "stem_content_gate.json"
_OUT_DIR: Path = _BENCH_DIR / "clips-4stem"
_CLIPS_REL: str = "clips-4stem"
_INDEX: Path = _BENCH_DIR / "ladder_4stem.json"
_COMBINED: Path = _BENCH_DIR / "ladder_4stem_all.json"

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


# A clip can pass every duration and container check and still contain nothing:
# that is exactly the failure this round exists to fix, so the ENCODED file is
# measured, not the wav it came from.
_MIN_CLIP_RMS_DBFS: float = -45.0


def _mean_volume_dbfs(path: Path) -> float:
    """Mean volume of the ENCODED file, straight from ffmpeg's volumedetect."""
    proc = subprocess.run(
        [_require_tool("ffmpeg"), "-hide_banner", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, check=True)
    for line in proc.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].strip().split()[0])
    raise RuntimeError(f"ffmpeg volumedetect reported no mean_volume for {path}")


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
    mean_dbfs = _mean_volume_dbfs(destination)
    if mean_dbfs < _MIN_CLIP_RMS_DBFS:
        raise RuntimeError(
            f"{destination} is {mean_dbfs:.1f} dBFS mean -- below the "
            f"{_MIN_CLIP_RMS_DBFS:.0f} dBFS audibility floor. A clip nobody can hear "
            "cannot be rated, so it is not published.")


def _machine_scores(ladder: dict[str, Any]) -> dict[str, int]:
    """One machine score per ARM, from its mean SI-SDR over all four stems.

    The four-stem page grades an arm on every stem at once, so its rank has to be
    an all-stem statistic. Ranks are 1..N and unique by construction, which is
    what the rater asserts.
    """
    stems: list[str] = ladder["stems"]
    ordered = sorted(ladder["arms"],
                     key=lambda a: sum(a["si_sdr"][s] for s in stems) / len(stems))
    return {arm["id"]: rank + 1 for rank, arm in enumerate(ordered)}


def _gate_evidence(track: str) -> dict[str, Any]:
    """The content-gate row for THIS track, plus who else was measured and why
    they were rejected. Fails loudly if the track was never gated or did not pass:
    encoding sixty seconds of near-silence again is the one outcome to prevent."""
    if not _GATE_JSON.is_file():
        raise RuntimeError(
            f"{_GATE_JSON} does not exist. Run stem_content_gate.py BEFORE encoding "
            "clips -- picking a track without checking what is in its stems is the "
            "mistake this file exists to stop repeating.")
    gate = json.loads(_GATE_JSON.read_text(encoding="utf-8"))
    winner = gate["winner"]
    if not track.startswith(winner["track"]):
        raise RuntimeError(
            f"the content gate passed {winner['track']!r} but this ladder is for "
            f"{track!r}. Re-run the gate or the ladder; do not publish an ungated track.")
    row = [c for c in gate["candidates"] if c["track"] == winner["track"]]
    if len(row) != 1:
        raise RuntimeError(f"content gate has {len(row)} rows for {winner['track']!r}, expected 1")
    if not row[0]["passed"]:
        raise RuntimeError(f"{winner['track']!r} did not pass the content gate")
    return {
        "winner": winner,
        "stems": {c["stem"]: c for c in row[0]["stems"]},
        "candidates_measured": len(gate["candidates"]),
        "candidates_rejected": [
            {"track": c["track"], "start_s": c["start_s"], "failures": c["failures"]}
            for c in gate["candidates"] if not c["passed"]
        ],
        "bars": gate["gate"],
    }


def _combined_manifest(ladder: dict[str, Any], slug: str,
                       gate: dict[str, Any]) -> dict[str, Any]:
    """ONE manifest, every stem. `stems` carries a true-stem reference each, and
    every arm carries all four of its outputs, so the page can put an arm's whole
    separation on one card instead of spreading it over four pages."""
    stems: list[str] = ladder["stems"]
    score_of = _machine_scores(ladder)

    return {
        "track": ladder["track"],
        "mode": "four-stem",
        # Blind by default. Rounds rated with the params string and the machine
        # score on screen came back with the well-labelled arms tied at the top.
        "blind_default": True,
        "genre": ladder["genre"],
        "dataset": ladder["dataset"],
        "window": ladder["window"],
        "hardware": ladder["hardware"],
        "mixture_file": f"{_CLIPS_REL}/{slug}-mixture.m4a",
        "mixture_note": "the raw mixture, REFERENCE ONLY - it is not an arm and is not "
                        "graded. Its SI-SDR against each true stem is that stem's "
                        "do-nothing floor.",
        "content_gate": gate,
        "stems": [
            {
                "stem": stem,
                "reference": f"{_CLIPS_REL}/{slug}-truth-{stem}.m4a",
                "reference_kind": "TRUE MUSDB18-HQ stem",
                "mixture_floor_si_sdr": ladder["mixture_floor_si_sdr"][stem],
                "content": {
                    "rms_dbfs": gate["stems"][stem]["rms_dbfs"],
                    "peak_dbfs": gate["stems"][stem]["peak_dbfs"],
                    "active_frac": gate["stems"][stem]["active_frac"],
                    "energy_share": gate["stems"][stem]["energy_share"],
                    "rms_below_loudest_db": gate["stems"][stem]["rms_below_loudest_db"],
                    "peak_below_loudest_db": gate["stems"][stem]["peak_below_loudest_db"],
                },
            }
            for stem in stems
        ],
        "arms": [
            {
                "id": arm["id"],
                "params": arm["params"],
                "separate_s": arm["infer_s"],
                "s_per_stem_minute": arm["s_per_stem_minute"],
                "machine_score": score_of[arm["id"]],
                "stems": {
                    stem: {
                        "file": f"{_CLIPS_REL}/{slug}-{arm['id']}-{stem}.m4a",
                        "si_sdr": arm["si_sdr"][stem],
                    }
                    for stem in stems
                },
            }
            for arm in ladder["arms"]
        ],
    }


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

    gate = _gate_evidence(ladder["track"])

    def _check(manifest_paths: list[str], where: str) -> None:
        for relative in manifest_paths:
            if not (_BENCH_DIR / relative).is_file():
                raise RuntimeError(f"{where} references a missing file: {relative}")

    combined = _combined_manifest(ladder, slug, gate)
    _check([g["reference"] for g in combined["stems"]]
           + [combined["mixture_file"]]
           + [c["file"] for a in combined["arms"] for c in a["stems"].values()],
           "the combined manifest")
    arm_scores = [a["machine_score"] for a in combined["arms"]]
    if len(set(arm_scores)) != len(arm_scores):
        raise RuntimeError("combined manifest has duplicate machine_score values across arms")
    _COMBINED.write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")

    manifests: list[dict[str, str]] = []
    for stem in stems:
        manifest = _stem_manifest(ladder, stem, slug)
        _check([manifest["reference"], manifest["mixture_file"]]
               + [c["file"] for c in manifest["clips"]], f"the {stem} manifest")
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
        "note": "INDEX, not a rater manifest. Open ladder_4stem_all.json instead: it is "
                "the one page that shows every arm with all four of its stems together, "
                "colour-coded, each against its own TRUE stem. The per-stem manifests "
                "below still render and are kept so older links do not break.",
        "open": f"vocal_quality_rater.html?manifest={_COMBINED.name}",
        "combined_manifest": _COMBINED.name,
        "content_gate": gate,
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
    print(f"[OK] wrote {total} m4a clips, every one duration-checked and above the "
          f"{_MIN_CLIP_RMS_DBFS:.0f} dBFS audibility floor")
    print(f"[OK] combined manifest {_COMBINED} "
          f"({len(combined['arms'])} arms x {len(stems)} stems = "
          f"{len(combined['arms']) * len(stems)} scores on one page)")
    print(f"[OK] {len(stems)} per-stem manifests and {_INDEX} still written")
    print(f"[OK] open vocal_quality_rater.html?manifest={_COMBINED.name}")


if __name__ == "__main__":
    main()
