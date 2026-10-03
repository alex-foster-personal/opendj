# /// script
# requires-python = ">=3.11"
# dependencies = ["tensorflow", "numpy<2", "soundfile", "scipy"]
# ///
"""Run a user-supplied Spleeter 4stems SavedModel (optional comparison arm).

The model directory is supplied by the caller via --model (or the
RB6_SPLEETER_MODEL env var); nothing is fetched or located automatically, and a
missing or invalid path fails loudly. The graph takes a complex STFT and returns four masked complex STFTs; spleeter's own 4stems config
fixes frame_length 4096, frame_step 1024, T 512, F 1024, mask_extension zeros.

--sep-rate resamples before separation and back after, so the cost of the
resample is charged to the arm rather than hidden.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import tensorflow as tf
from scipy.signal import resample_poly

FRAME_LENGTH, FRAME_STEP, T, F = 4096, 1024, 512, 1024
STEMS = ("vocals", "drums", "bass", "other")


def _hann(fl, dtype):
    return tf.signal.hann_window(fl, periodic=True, dtype=dtype)


def _stft(x: np.ndarray) -> tf.Tensor:
    s = tf.signal.stft(tf.transpose(x), FRAME_LENGTH, FRAME_STEP,
                       window_fn=_hann, pad_end=True)
    return tf.transpose(s, perm=[1, 2, 0])  # (frames, 2049, ch)


def _istft(s: tf.Tensor, n: int) -> np.ndarray:
    s = tf.transpose(s, perm=[2, 0, 1])
    y = tf.signal.inverse_stft(
        s, FRAME_LENGTH, FRAME_STEP,
        window_fn=tf.signal.inverse_stft_window_fn(FRAME_STEP, forward_window_fn=_hann))
    return tf.transpose(y).numpy()[:n]


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return x
    g = np.gcd(src, dst)
    return resample_poly(x, dst // g, src // g, axis=0).astype(np.float32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--sep-rate", type=int, default=44100)
    ap.add_argument("--mono", action="store_true")
    ap.add_argument("--label", required=True)
    ap.add_argument("--model", type=Path, default=None,
                    help="Spleeter 4stems SavedModel directory (env: RB6_SPLEETER_MODEL)")
    args = ap.parse_args()
    model_dir = args.model or (
        Path(os.environ["RB6_SPLEETER_MODEL"]) if os.environ.get("RB6_SPLEETER_MODEL") else None)
    if model_dir is None:
        ap.error("a model path is required: pass --model or set RB6_SPLEETER_MODEL")
    if not (model_dir / "saved_model.pb").is_file():
        ap.error(f"not a SavedModel directory (no saved_model.pb): {model_dir}")

    wav, sr = sf.read(str(args.input), dtype="float32", always_2d=True)
    if wav.shape[1] == 1:
        wav = np.repeat(wav, 2, axis=1)
    orig_n = wav.shape[0]

    work = _resample(wav, sr, args.sep_rate)
    if args.mono:
        work = np.repeat(work.mean(axis=1, keepdims=True), 2, axis=1)
    n = work.shape[0]

    fn = tf.saved_model.load(str(model_dir)).signatures["serving_default"]

    t0 = time.perf_counter()
    spec = _stft(work)
    frames, bins, _ = spec.shape
    cropped = spec[:, :F, :]
    pad = (-frames) % T
    segs = tf.reshape(tf.pad(cropped, [[0, pad], [0, 0], [0, 0]]), (-1, T, F, 2))

    acc: dict[str, list] = {k: [] for k in STEMS}
    for i in range(segs.shape[0]):
        out = fn(mix_stft=segs[i:i + 1])
        for k in acc:
            acc[k].append(out[k][0])

    stems = {}
    for k, parts in acc.items():
        m = tf.concat(parts, axis=0)[:frames]
        full = tf.concat([m, tf.zeros((frames, bins - F, 2), tf.complex64)], axis=1)
        stems[k] = _istft(full, n)
    infer_s = time.perf_counter() - t0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    # All four stems, not just vocals: this is a 4stems model and the other three
    # were being discarded, which is exactly the blind spot the 4-stem ladder exists
    # to close.
    for name in STEMS:
        sf.write(str(args.out_dir / f"{name}.wav"),
                 _resample(stems[name], args.sep_rate, sr)[:orig_n], sr)
    inst = _resample(stems["drums"] + stems["bass"] + stems["other"],
                     args.sep_rate, sr)[:orig_n]
    sf.write(str(args.out_dir / "instrumental.wav"), inst, sr)

    dur = orig_n / sr
    print(json.dumps({
        "label": args.label, "engine": "user-supplied-spleeter-4stems",
        "sep_rate": args.sep_rate, "mono": args.mono,
        "audio_s": round(dur, 2), "infer_s": round(infer_s, 2),
        "s_per_stem_minute": round(infer_s / (dur / 60), 2),
    }))


if __name__ == "__main__":
    main()
