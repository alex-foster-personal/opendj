"""MUSDB18-HQ ABSOLUTE leaderboard: the 10-rung ladder vs TRUE vocal stems.

scripts/modal_vocal_ladder.py scores every rung against rung 10 (a PSEUDO
reference), so any rung sharing rung 10's model family scores well by family
resemblance alone -- htdemucs_ft rungs are graded against an htdemucs_ft
reference. This script removes that circularity: it runs the SAME ten rungs
(imported, not re-declared) over MUSDB18-HQ mixtures whose ground-truth vocal
stem is known, and scores SI-SDR against that truth. That is the absolute
number, and it is what settles whether htdemucs_ft earns its ~4x compute.

Method per track:
  1. the 75s excerpt window is chosen from the TRUE vocals (max vocal energy,
     scripts/bench/pick_vocal_window.py) -- never from a separator's guess;
  2. the IDENTICAL window is ffmpeg-cut from the mixture and from the truth,
     so reference and estimate are sample-aligned (a window mismatch would
     silently destroy every score);
  3. all 10 rungs run on the mixture excerpt via ladder_separate on an L4;
  4. SI-SDR vs the truth excerpt, locally, via scripts/bench/si_sdr_score.py.

Requirements (mini-PRD):
  ✔︎ ✅ reuses modal_vocal_ladder's LADDER + ladder_separate + image (no rebuild,
    no second copy of the rung spec).
    [if] the rung spec ever changes [then] this leaderboard changes with it
  ✔︎ ✅ every input file is duration-probed, not merely existence-checked, and
    mixture/truth durations must agree.
    [if] a stem is truncated relative to its mixture [then ⛔️] RuntimeError
  ✔︎ ✅ writes scripts/bench/ladder_musdb.json in the rater's manifest shape
    (top level = track 1, plus a `tracks` array carrying every track) and clips
    to scripts/bench/clips-musdb/<slug>-rung-NN.flac so the run is auditionable.
    [if] a clip is missing after the run [then ⛔️] RuntimeError
  ✔︎ ✅ prints per-track tables carrying BOTH the true SI-SDR and the pseudo
    SI-SDR from ladder.json, then the htdemucs_ft-vs-htdemucs family verdict.
    [if] ladder.json is absent [then] the pseudo column prints "n/a", not a crash

Run (from the repo root; the shared app carries both entrypoints, so name this
one -- the imported module's `main` is the pseudo-reference ladder):
  modal run scripts/modal_musdb_ladder.py::musdb

-Claude
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.modal_vocal_ladder import (
    _BENCH_DIR,
    _LADDER_JSON,
    EXCERPT_LEN_S,
    LADDER,
    _ffmpeg_excerpt_flac,
    _ffprobe_duration_s,
    _require_tool,
    _si_sdr_scores,
    _write_clips,
    app,
    ladder_separate,
)

_MUSDB_DIR: Path = _BENCH_DIR.parent.parent / ".tmp/bench/musdb-hq/musdb-extract"
_CLIPS_DIR: Path = _BENCH_DIR / "clips-musdb"
_OUT_JSON: Path = _BENCH_DIR / "ladder_musdb.json"
_WINDOW_PICKER: Path = _BENCH_DIR / "pick_vocal_window.py"

# The two canonical MUSDB18-HQ test tracks. Names are the on-disk stems produced
# by the bifrost2 zip extraction (<Track>_mixture.wav / <Track>_vocals.wav).
TRACKS: tuple[str, ...] = ("Al James - Schoolboy Facination", "Zeno - Signs")

# Duration agreement tolerance between a mixture and its vocal stem. MUSDB18-HQ
# ships them sample-identical; anything above a hop is a truncated download.
_DURATION_TOL_S: float = 0.05


@dataclass(frozen=True)
class TrackRun:
    """One track's finished ladder: window evidence + per-rung outcomes."""

    title: str
    slug: str
    window: dict[str, float]
    results: list[dict[str, Any]]
    sisdr_by_rung: dict[str, float]

    @property
    def truth_clip(self) -> str:
        return f"{_CLIPS_DIR.name}/{self.slug}-truth-vocals.flac"


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _pick_window(vocals: Path) -> dict[str, float]:
    """Delegate window choice to the uv PEP 723 picker (keeps numpy out of here)."""
    if not _WINDOW_PICKER.is_file():
        raise RuntimeError(f"window picker missing: {_WINDOW_PICKER}")
    completed = subprocess.run(
        [
            _require_tool("uv"),
            "run",
            "--quiet",
            str(_WINDOW_PICKER),
            "--audio",
            str(vocals),
            "--length",
            str(EXCERPT_LEN_S),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _resolve_pair(title: str) -> tuple[Path, Path]:
    """Mixture + truth vocals for a track, duration-probed (not just existence)."""
    mixture = _MUSDB_DIR / f"{title}_mixture.wav"
    vocals = _MUSDB_DIR / f"{title}_vocals.wav"
    for path in (mixture, vocals):
        if not path.is_file():
            raise RuntimeError(f"MUSDB18-HQ input missing: {path}")
    mixture_s = _ffprobe_duration_s(mixture)
    vocals_s = _ffprobe_duration_s(vocals)
    if abs(mixture_s - vocals_s) > _DURATION_TOL_S:
        raise RuntimeError(
            f"{title}: mixture {mixture_s:.3f}s != vocals {vocals_s:.3f}s -- one of "
            "the two is truncated, scoring against it would be meaningless"
        )
    if mixture_s <= EXCERPT_LEN_S:
        raise RuntimeError(
            f"{title}: track is {mixture_s:.1f}s, shorter than the {EXCERPT_LEN_S}s excerpt"
        )
    print(f"{title}: mixture+vocals both {mixture_s:.2f}s (44.1 kHz stereo)")
    return mixture, vocals


def _pseudo_sisdr() -> dict[str, float | None]:
    """rung id -> SI-SDR from the pseudo-reference run, or {} when absent."""
    if not _LADDER_JSON.is_file():
        return {}
    manifest = json.loads(_LADDER_JSON.read_text(encoding="utf-8"))
    return {clip["id"]: clip.get("si_sdr") for clip in manifest["clips"]}


def _run_track(title: str) -> TrackRun:
    mixture_path, vocals_path = _resolve_pair(title)
    slug = _slug(title)

    window = _pick_window(vocals_path)
    print(
        f"{title}: CHOSEN WINDOW [{window['start_s']:.1f}s -> {window['end_s']:.1f}s] "
        f"chosen on TRUE vocal energy, rms={window['rms_dbfs']} dBFS, "
        f"silent hops {100 * window['silent_frac']:.1f}%"
    )

    start_s, length_s = window["start_s"], window["length_s"]
    mixture_excerpt = _ffmpeg_excerpt_flac(mixture_path, start_s, length_s)
    truth_excerpt = _ffmpeg_excerpt_flac(vocals_path, start_s, length_s)

    _CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    truth_path = _CLIPS_DIR / f"{slug}-truth-vocals.flac"
    mix_path = _CLIPS_DIR / f"{slug}-mixture.flac"
    truth_path.write_bytes(truth_excerpt)
    mix_path.write_bytes(mixture_excerpt)
    for path in (truth_path, mix_path):
        cut_s = _ffprobe_duration_s(path)
        if abs(cut_s - length_s) > _DURATION_TOL_S:
            raise RuntimeError(f"{path} cut to {cut_s:.3f}s, expected {length_s:.3f}s")
    print(f"{title}: excerpts cut, mixture bytes={len(mixture_excerpt)}")

    args = [
        (mixture_excerpt, f"{rung.id}.flac", rung.model, rung.overlap, rung.shifts)
        for rung in LADDER
    ]
    results = list(ladder_separate.starmap(args))
    clips = _write_clips(results, prefix=f"{slug}-", out_dir=_CLIPS_DIR)
    raw = _si_sdr_scores(truth_path, clips)
    sisdr_by_rung = {
        Path(path).stem.replace(f"{slug}-", ""): value for path, value in raw.items()
    }
    missing = {rung.id for rung in LADDER} - set(sisdr_by_rung)
    if missing:
        raise RuntimeError(f"{title}: no SI-SDR returned for {sorted(missing)}")
    return TrackRun(title, slug, window, results, sisdr_by_rung)


# ----- reporting --------------------------------------------------------------


def _family(model: str) -> str:
    return "htdemucs_ft" if model == "htdemucs_ft" else model


def _print_track_table(run: TrackRun, pseudo: dict[str, float | None]) -> None:
    print(f"\nMUSDB18-HQ ABSOLUTE LADDER -- {run.title}")
    print("-" * 96)
    print(
        f"{'rung':<8}{'params':<34}{'sep_s':>8}{'SI-SDR vs TRUE':>16}{'pseudo SI-SDR':>15}"
    )
    print("-" * 96)
    for rung, result in zip(LADDER, run.results, strict=False):
        truth = run.sisdr_by_rung[rung.id]
        pseudo_value = pseudo.get(rung.id, "missing")
        if pseudo_value is None:
            pseudo_str = "ref"
        elif isinstance(pseudo_value, (int, float)):
            pseudo_str = f"{pseudo_value:.2f}"
        else:
            pseudo_str = "n/a"
        print(
            f"{rung.id:<8}{rung.params:<34}{result['separate_s']:>8.1f}"
            f"{truth:>16.2f}{pseudo_str:>15}"
        )
    print("-" * 96)
    best = max(run.sisdr_by_rung.items(), key=lambda kv: kv[1])
    print(f"best rung: {best[0]} at {best[1]:.2f} dB vs TRUE")


def _family_verdict(runs: list[TrackRun]) -> None:
    plain = [rung for rung in LADDER if rung.model == "htdemucs"]
    finetuned = [rung for rung in LADDER if rung.model == "htdemucs_ft"]
    print("\nFAMILY VERDICT (htdemucs_ft vs plain htdemucs, vs TRUE references)")
    print("-" * 96)
    for run in runs:
        plain_scores = [run.sisdr_by_rung[r.id] for r in plain]
        ft_scores = [run.sisdr_by_rung[r.id] for r in finetuned]
        plain_time = sum(res["separate_s"] for r, res in zip(LADDER, run.results, strict=False) if r.model == "htdemucs") / len(plain)
        ft_time = sum(res["separate_s"] for r, res in zip(LADDER, run.results, strict=False) if r.model == "htdemucs_ft") / len(finetuned)
        print(
            f"{run.title}\n"
            f"  htdemucs    (rungs 3-6):  best {max(plain_scores):6.2f} dB  "
            f"mean {sum(plain_scores) / len(plain_scores):6.2f} dB  "
            f"spread {max(plain_scores) - min(plain_scores):.2f} dB  "
            f"mean sep {plain_time:.1f}s\n"
            f"  htdemucs_ft (rungs 7-10): best {max(ft_scores):6.2f} dB  "
            f"mean {sum(ft_scores) / len(ft_scores):6.2f} dB  "
            f"spread {max(ft_scores) - min(ft_scores):.2f} dB  "
            f"mean sep {ft_time:.1f}s\n"
            f"  DELTA best-vs-best: {max(ft_scores) - max(plain_scores):+.2f} dB   "
            f"compute ratio: {ft_time / plain_time:.2f}x"
        )
    print("-" * 96)


def _write_manifest(runs: list[TrackRun], pseudo: dict[str, float | None]) -> None:
    """Rater-shaped manifest: top level is track 1, `tracks` carries every track."""

    def track_block(run: TrackRun) -> dict[str, Any]:
        by_rung = sorted(run.sisdr_by_rung.items(), key=lambda kv: kv[1])
        machine_scores = {rung_id: pos + 1 for pos, (rung_id, _) in enumerate(by_rung)}
        return {
            "track": f"{run.title} (MUSDB18-HQ)",
            "reference": run.truth_clip,
            "window": run.window,
            "mixture_file": f"{_CLIPS_DIR.name}/{run.slug}-mixture.flac",
            "clips": [
                {
                    "id": rung.id,
                    "file": f"{_CLIPS_DIR.name}/{run.slug}-{rung.id}.flac",
                    "inst_file": f"{_CLIPS_DIR.name}/{run.slug}-{rung.id}.inst.flac",
                    "machine_score": machine_scores[rung.id],
                    "params": rung.params,
                    "separate_s": result["separate_s"],
                    "si_sdr": run.sisdr_by_rung[rung.id],
                    "si_sdr_pseudo": pseudo.get(rung.id),
                }
                for rung, result in zip(LADDER, run.results, strict=False)
            ],
        }

    blocks = [track_block(run) for run in runs]
    manifest = dict(blocks[0])
    manifest["reference_kind"] = "true-stem"
    manifest["dataset"] = "MUSDB18-HQ test"
    manifest["tracks"] = blocks
    _OUT_JSON.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {_OUT_JSON}")


@app.local_entrypoint()
def musdb() -> None:
    pseudo = _pseudo_sisdr()
    if not pseudo:
        print(f"[WARN] {_LADDER_JSON} absent -- pseudo-reference column will read n/a")
    runs = [_run_track(title) for title in TRACKS]
    for run in runs:
        _print_track_table(run, pseudo)
    _family_verdict(runs)
    _write_manifest(runs, pseudo)
    print(f"wrote {2 * len(LADDER) * len(runs)} clips to {_CLIPS_DIR}")
