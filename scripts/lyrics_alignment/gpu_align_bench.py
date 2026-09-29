# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#   "torch==2.6.0",
#   "torchaudio==2.6.0",
#   "numpy",
#   "ctc-forced-aligner @ git+https://github.com/MahmoudAshraf97/ctc-forced-aligner.git@11855d1de76af2b490dd2e8e2db2661805ae90a0",
#   "stable-ts>=2.17",
# ]
#
# [[tool.uv.index]]
# name = "pytorch-cu124"
# url = "https://download.pytorch.org/whl/cu124"
# explicit = true
#
# [tool.uv.sources]
# torch = { index = "pytorch-cu124" }
# torchaudio = { index = "pytorch-cu124" }
# ///
"""Word-aligner bake-off on a local CUDA GPU: same songs, same stems, same scorer, many models.

The lyric word aligner today is MMS-300M, whose weights are CC-BY-NC 4.0 and so
outside the shipping standard (specs/native-analysis-v1.md, NATIVE-08). This runs
the permissive candidates the licence survey shortlisted against MMS on the SAME
vocal stems, so the choice of replacement rests on one measurement rather than a
CPU full-mix hint. Scoring is the repo's own: `python -m apps.lyrics score` and
`scripts.lyrics_alignment.measure_jamendo` read the prediction dirs this writes.

Requirements
- ✔︎ One CLI, one candidate per run, chosen by name from CANDIDATES (model id,
  weights licence and backend are recorded, never inferred at scoring time).
    [if] an unknown candidate is named [then ⛔️] exit before loading anything
    [if] cuda is unavailable [then ⛔️] hard error; this is the GPU bench
- ✔︎ Same prediction contract as scripts/modal_align_spike.py:
  <pred_dir>/<name>.json = {"words": [{word, start_s, end_s, score}]}, one entry per
  reference word, in order.
    [if] a backend returns a different word count [then ⛔️] that song errors
    [if] any song errored [then] exit 1 with every error listed
- ✔︎ CTC candidates run through the SAME aligner code and settings as today's
  MMS path (ctc-forced-aligner pin, float32, star_frequency=segment), so only the
  acoustic model differs between CTC rows.
    [if] a song's language has no model in the candidate [then] skipped, named, and
         counted in the manifest; never silently aligned with another language's model
- ✔︎ A manifest next to the pred dir, <pred_dir>.run.json: candidate, model ids,
  licence, device, per-song GPU seconds, skips and errors.

Usage (repo root; stems from gpu_separate_vocals.py):
  uv run --script scripts/lyrics_alignment/gpu_align_bench.py --candidate mms \\
      --audio-dir data/datasets/jamendolyrics-vocals-local --pred-dir data/state/lyrics-eval/gpu-mms
  uv run --no-sync python -m apps.lyrics score --pred data/state/lyrics-eval/gpu-mms

-Claude
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import torch

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "data" / "datasets" / "jamendolyrics"
SAMPLE_RATE = 16_000
STAR_TOKEN = "<star>"
STAR_FREQUENCY = "segment"
CTC_BATCH_SIZE = 4
LANG_TO_ISO639_3 = {"English": "eng", "German": "deu", "Spanish": "spa", "French": "fra"}
LANG_TO_ISO639_1 = {"English": "en", "German": "de", "Spanish": "es", "French": "fr"}


@dataclass(frozen=True)
class Candidate:
    """One aligner under test. `models` maps a JamendoLyrics language to a model id."""

    backend: str  # "ctc" | "whisper"
    models: dict[str, str]
    weights_license: str
    note: str = ""


MMS = "MahmoudAshraf/mms-300m-1130-forced-aligner"
CANDIDATES: dict[str, Candidate] = {
    "mms": Candidate(
        "ctc",
        dict.fromkeys(LANG_TO_ISO639_3, MMS),
        "CC-BY-NC-4.0",
        "today's aligner; the baseline, not a shipping option",
    ),
    "w2v2-lv60-en": Candidate(
        "ctc", {"English": "facebook/wav2vec2-large-960h-lv60-self"}, "Apache-2.0", "English only"
    ),
    "xlsr-grosman": Candidate(
        "ctc",
        {
            "English": "jonatasgrosman/wav2vec2-large-xlsr-53-english",
            "German": "jonatasgrosman/wav2vec2-large-xlsr-53-german",
            "Spanish": "jonatasgrosman/wav2vec2-large-xlsr-53-spanish",
            "French": "jonatasgrosman/wav2vec2-large-xlsr-53-french",
        },
        "Apache-2.0",
        "one fine-tuned XLSR-53 per language, Common Voice",
    ),
    "permissive-best": Candidate(
        "ctc",
        {
            "English": "facebook/wav2vec2-large-960h-lv60-self",
            "German": "jonatasgrosman/wav2vec2-large-xlsr-53-german",
            "Spanish": "jonatasgrosman/wav2vec2-large-xlsr-53-spanish",
            "French": "jonatasgrosman/wav2vec2-large-xlsr-53-french",
        },
        "Apache-2.0",
        "bake-off winner per language (Tue 29 Sep 2026): lv60 for English, XLSR-53 otherwise",
    ),
    "whisper-turbo": Candidate(
        "whisper",
        dict.fromkeys(LANG_TO_ISO639_3, "large-v3-turbo"),
        "MIT",
        "stable-ts align() over Whisper cross-attention, no second model",
    ),
}

# -----------------------------------------------------------------------------


@dataclass
class Manifest:
    candidate: str
    backend: str
    models: dict[str, str]
    weights_license: str
    device: str
    star_frequency: str = STAR_FREQUENCY
    gpu_s: dict[str, float] = field(default_factory=dict)
    skipped_no_model: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    load_s: dict[str, float] = field(default_factory=dict)


def _dataset_rows() -> list[dict[str, str]]:
    with (DATASET_DIR / "JamendoLyrics.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _reference_words(name: str) -> list[str]:
    raw = (DATASET_DIR / "lyrics" / f"{name}.words.txt").read_text(encoding="utf-8")
    return [w for w in raw.split("\n") if w.strip()]


def _decode_16k_mono(path: Path) -> np.ndarray:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed on {path}: {proc.stderr.decode()[:300]}")
    audio = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    if audio.size < SAMPLE_RATE:
        raise RuntimeError(f"decoded <1s of audio from {path}")
    return audio


# ----- backends ---------------------------------------------------------------

Aligner = Callable[[np.ndarray, list[str], str], list[dict[str, Any]]]


def _ctc_aligner(model_id: str) -> Aligner:
    from ctc_forced_aligner import (
        generate_emissions,
        get_alignments,
        get_spans,
        load_alignment_model,
        postprocess_results,
        preprocess_text,
    )

    model, tokenizer = load_alignment_model("cuda", model_path=model_id, dtype=torch.float32)

    def align(audio: np.ndarray, words: list[str], language: str) -> list[dict[str, Any]]:
        waveform = torch.from_numpy(audio).to(model.dtype).to(model.device)
        emissions, stride = generate_emissions(model, waveform, batch_size=CTC_BATCH_SIZE)
        tokens, text = preprocess_text(
            " ".join(words),
            romanize=True,
            language=LANG_TO_ISO639_3[language],
            star_frequency=STAR_FREQUENCY,
        )
        segments, scores, blank = get_alignments(emissions, tokens, tokenizer)
        stamps = postprocess_results(text, get_spans(tokens, segments, blank), stride, scores)
        stamps = [s for s in stamps if s["text"] != STAR_TOKEN]
        if len(stamps) != len(words):
            raise RuntimeError(
                f"aligner returned {len(stamps)} words for {len(words)} reference words"
            )
        return [
            {"word": w, "start_s": s["start"], "end_s": s["end"], "score": s["score"]}
            for w, s in zip(words, stamps, strict=True)
        ]

    return align


def _whisper_aligner(model_id: str) -> Aligner:
    import stable_whisper

    model = stable_whisper.load_model(model_id, device="cuda")

    def align(audio: np.ndarray, words: list[str], language: str) -> list[dict[str, Any]]:
        # suppress_silence off: on a vocal stem the instrumental gaps read as silence and
        # stable-ts packs early words into them (one stem measured 67 s median error with it
        # on, word onsets within MMS's with it off).
        result = model.align(
            audio, " ".join(words), language=LANG_TO_ISO639_1[language], suppress_silence=False
        )
        got = [w for seg in result.segments for w in seg.words]
        if len(got) != len(words):
            raise RuntimeError(
                f"stable-ts returned {len(got)} words for {len(words)} reference words"
            )
        return [
            {"word": w, "start_s": g.start, "end_s": g.end, "score": g.probability}
            for w, g in zip(words, got, strict=True)
        ]

    return align


BACKENDS: dict[str, Callable[[str], Aligner]] = {"ctc": _ctc_aligner, "whisper": _whisper_aligner}

# -----------------------------------------------------------------------------


def run(
    candidate_name: str, audio_dir: Path, audio_template: str, pred_dir: Path, limit: int
) -> Manifest:
    candidate = CANDIDATES[candidate_name]
    manifest = Manifest(
        candidate_name,
        candidate.backend,
        candidate.models,
        candidate.weights_license,
        torch.cuda.get_device_name(0),
    )
    pred_dir.mkdir(parents=True, exist_ok=True)
    aligners: dict[str, Aligner] = {}
    for row in _dataset_rows()[: limit or None]:
        name, language = Path(row["Filepath"]).stem, row["Language"]
        model_id = candidate.models.get(language)
        if model_id is None:
            manifest.skipped_no_model.append(name)
            print(f"[SKIP-LANG] {name} ({language})", flush=True)
            continue
        if model_id not in aligners:
            t_load = time.perf_counter()
            aligners[model_id] = BACKENDS[candidate.backend](model_id)
            manifest.load_s[model_id] = round(time.perf_counter() - t_load, 2)
        audio_path = audio_dir / audio_template.format(name=name)
        t0 = time.perf_counter()
        try:
            words = aligners[model_id](
                _decode_16k_mono(audio_path), _reference_words(name), language
            )
        except Exception as exc:  # per-song isolation; every error is listed and fails the run
            manifest.errors[name] = f"{type(exc).__name__}: {exc}"
            print(f"[ERR] {name} {manifest.errors[name]}", flush=True)
            continue
        torch.cuda.synchronize()
        manifest.gpu_s[name] = round(time.perf_counter() - t0, 3)
        (pred_dir / f"{name}.json").write_text(
            json.dumps({"words": words}, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[ok] {name} {len(words)}w {manifest.gpu_s[name]}s", flush=True)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidate", required=True, choices=sorted(CANDIDATES))
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--audio-template", default="{name}-vocals.flac")
    parser.add_argument("--pred-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if not torch.cuda.is_available():
        raise SystemExit("[ERROR] cuda unavailable; this runner is GPU-only by design")

    t_run = time.perf_counter()
    manifest = run(args.candidate, args.audio_dir, args.audio_template, args.pred_dir, args.limit)
    Path(f"{args.pred_dir}.run.json").write_text(
        json.dumps(asdict(manifest), indent=2), encoding="utf-8"
    )
    print(
        f"[total] {len(manifest.gpu_s)} aligned, {len(manifest.errors)} errors, "
        f"{len(manifest.skipped_no_model)} skipped (no model), "
        f"wall {time.perf_counter() - t_run:.0f}s"
    )
    return 1 if manifest.errors else 0


if __name__ == "__main__":
    sys.exit(main())
