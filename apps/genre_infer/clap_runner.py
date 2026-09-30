#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#     "transformers==5.17.0",
#     "torch==2.8.0",
#     "librosa==0.11.0",
#     "numpy==2.4.6",
# ]
# ///
"""LAION CLAP audio embeddings, one JSON document per track.

WHICH MODEL AND WHY. `laion/larger_clap_music_and_speech` is Apache-2.0,
trained on music among other audio, and loads through `transformers` with no
extra package. Microsoft's
CLAP weights are MS-PL and the Essentia/MTG genre models, MERT and Last.fm
tags are non-commercial, so none of them can back a shipped feature. The
embedding is the reusable half: genre is a small classifier on top of it
(`apps/genre_infer/classify.py`), trained on the library's own tags, and the
same vector can later serve similarity search without re-running audio.

NOT `laion/larger_clap_music`, the obvious pick. Its published weights load
with no missing keys and return collapsed embeddings: any two clips, a sine
and white noise included, score about 0.96 cosine, and every clip scores
about 0.01 against every text prompt, under transformers 4.57 and 5.17
alike. A genre head trained on it scored near chance, which is how this was
found. `_self_test` below is the positive control that would have caught it
before a single track was embedded, so it runs on every start.

WHAT ONE EMBEDDING IS. Six 10 s windows spread evenly over the middle 80
percent of the track (skipping intros and outros that rarely carry the
genre), each embedded at 48 kHz, L2-normalized, then averaged and normalized
again. The per-window vectors are not kept: 6 x 512 floats per track across a
10k library is 120 MB for a signal the mean already carries.

Measured on a 4-core Linux CPU with one torch thread, Thu 24 Sep 2026: about
3.6 s per track including decode.

Usage (from the repo root, heavy deps stay out of the app venv):

    uv run --no-project --script apps/genre_infer/clap_runner.py \\
        --manifest tracks.jsonl --out embeddings.jsonl [--threads 2]

`tracks.jsonl` holds `{"stable_id": ..., "path": ...}` per line. Each output
line is `{"stable_id", "status", "vector"|"reason", "model", "revision"}`; a
track that fails says why and never emits a vector.

-Claude
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

MODEL = "laion/larger_clap_music_and_speech"
# Each synthetic clip must match its own prompt best, and two unlike clips
# must not look alike. Checked against the collapsed checkpoint: it fails both.
SELF_TEST_PROMPTS = ("a pure sine tone", "white noise")
SELF_TEST_MAX_CLIP_COSINE = 0.9
SAMPLE_RATE = 48_000
WINDOW_S = 10.0
WINDOWS = 6


def _vec(out: Any) -> Any:
    """transformers 5 returns an output object; the projected embedding is pooler_output."""
    return (out.pooler_output if hasattr(out, "pooler_output") else out).numpy()


def _self_test(model: Any, proc: Any) -> None:
    """Refuse to embed anything with a checkpoint that cannot tell a sine from noise."""
    import numpy as np
    import torch

    t = np.arange(int(SAMPLE_RATE * WINDOW_S)) / SAMPLE_RATE
    clips = [
        (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32),
        np.random.default_rng(0).normal(scale=0.3, size=t.size).astype(np.float32),
    ]
    with torch.no_grad():
        a = _vec(
            model.get_audio_features(
                **proc(audio=clips, sampling_rate=SAMPLE_RATE, return_tensors="pt")
            )
        )
        txt = _vec(
            model.get_text_features(
                **proc(text=list(SELF_TEST_PROMPTS), return_tensors="pt", padding=True)
            )
        )
    a = a / np.linalg.norm(a, axis=1, keepdims=True)
    txt = txt / np.linalg.norm(txt, axis=1, keepdims=True)
    sim = a @ txt.T
    clip_cos = float(a[0] @ a[1])
    shown = [[round(float(x), 3) for x in row] for row in sim]
    if list(sim.argmax(1)) != [0, 1] or clip_cos > SELF_TEST_MAX_CLIP_COSINE:
        raise RuntimeError(
            f"{MODEL} failed its self-test: audio-text similarity {shown} "
            f"(each row must peak on its own prompt {SELF_TEST_PROMPTS}), sine-vs-noise "
            f"cosine {clip_cos:.3f} (must be <= {SELF_TEST_MAX_CLIP_COSINE}). "
            "Embeddings from this checkpoint would be meaningless; refusing to run."
        )


def _embed(path: Path, model: Any, proc: Any) -> list[float]:
    import librosa
    import numpy as np
    import torch

    dur = librosa.get_duration(path=str(path))
    if dur < WINDOW_S:
        raise ValueError(f"track is {dur:.1f} s, shorter than one {WINDOW_S:.0f} s window")
    lo, hi = 0.1 * dur, max(0.1 * dur, 0.9 * dur - WINDOW_S)
    clips = []
    for o in np.linspace(lo, hi, WINDOWS):
        clip = librosa.load(
            str(path), sr=SAMPLE_RATE, mono=True, offset=float(o), duration=WINDOW_S
        )[0]
        # A truncated file keeps its full-length header, so the header duration
        # promises audio that is not there (3 of 281 byte-exact FMA files).
        if clip.size < 0.9 * SAMPLE_RATE * WINDOW_S:
            raise ValueError(
                f"window at {o:.0f} s decoded {clip.size / SAMPLE_RATE:.1f} s of audio; "
                f"the header says {dur:.0f} s, so the file is probably truncated"
            )
        clips.append(clip)
    with torch.no_grad():
        e = _vec(
            model.get_audio_features(
                **proc(audio=clips, sampling_rate=SAMPLE_RATE, return_tensors="pt")
            )
        )
    e = e / np.linalg.norm(e, axis=1, keepdims=True)
    m = e.mean(0)
    return [round(float(x), 6) for x in m / np.linalg.norm(m)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--threads", type=int, default=2)
    args = ap.parse_args(argv)

    import torch
    from huggingface_hub import model_info  # type: ignore[import-not-found]
    from transformers import ClapModel, ClapProcessor  # type: ignore[import-not-found]

    torch.set_num_threads(args.threads)
    revision = model_info(MODEL).sha
    model = ClapModel.from_pretrained(MODEL, revision=revision).eval()
    proc = ClapProcessor.from_pretrained(MODEL, revision=revision)
    _self_test(model, proc)
    tracks = [json.loads(line) for line in args.manifest.read_text().splitlines() if line.strip()]
    failed = 0
    t0 = time.perf_counter()
    with args.out.open("w") as fh:
        for t in tracks:
            row: dict[str, Any] = {
                "stable_id": t["stable_id"],
                "model": MODEL,
                "revision": revision,
            }
            try:
                path = Path(t["path"])
                if not path.is_file():
                    raise FileNotFoundError(f"audio not found: {path}")  # noqa: TRY301
                row.update(status="ok", vector=_embed(path, model, proc))
            except Exception as exc:  # noqa: BLE001 -- one bad file must not end the batch
                failed += 1
                row.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
            fh.write(json.dumps(row) + "\n")
            fh.flush()
    print(
        json.dumps(
            {
                "tracks": len(tracks),
                "failed": failed,
                "model": MODEL,
                "revision": revision,
                "seconds": round(time.perf_counter() - t0, 1),
            }
        ),
        file=sys.stderr,
    )
    return 1 if failed == len(tracks) and tracks else 0


if __name__ == "__main__":
    raise SystemExit(main())
