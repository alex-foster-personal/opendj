"""odj-live (apps/live-core) against librosa and the library's own key scorer.

Two claims, both measured on synthetic signals generated here (no network,
no committed fixture):

1. The live core's whole-track mean chroma has the SHAPE of
   ``librosa.feature.chroma_cqt``: cosine similarity of the two mean vectors
   is at least 0.98 on every signal.
2. Its whole-track key equals what ``apps/analysis_key/profiles.py``
   (``estimate_key_krumhansl``) says when it is handed librosa's chroma of
   the same audio, so the live key and the library key are one instrument.

Each signal is also a control for the others: the signals are in different
keys, so a constant answer cannot pass. The cosine figures print with ``-s``.

The binary is built from this checkout with cargo (``conftest.py``). A host
without cargo, librosa or soundfile reports UNAVAILABLE (a skip naming the
gap); the contracts job, which builds the crate, runs this with
``MDT_REQUIRE_LIVE_CORE_BUILD=1`` so any of those gaps fails there instead.
The same checks on the committed encoded-media fixture live in
``test_live_core_acceptance_media.py``.
"""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from apps.analysis_key import canon, profiles
from tests.live_core.conftest import AUDIO_STACK_MARKS, COSINE_FLOOR

RATE = 44100

# Applied only when MDT_REQUIRE_LIVE_CORE_BUILD is not 1 (see conftest.py):
# under the flag a missing audio stack fails in the librosa fixture instead of
# being skipped before it runs.
pytestmark = AUDIO_STACK_MARKS


def _midi_hz(midi: float, cents: float = 0.0) -> float:
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0 + cents / 1200.0)


def _note(buf: np.ndarray, midi: float, start_s: float, dur_s: float, amp: float, cents: float) -> None:
    start = int(start_s * RATE)
    n = min(int(dur_s * RATE), buf.size - start)
    t = np.arange(n) / RATE
    env = np.minimum(t / 0.01, 1.0) * (0.5 + 0.5 * np.exp(-t / dur_s * 2.0)) * np.clip((dur_s - t) / 0.03, 0.0, 1.0)
    f = _midi_hz(midi, cents)
    tone = sum(np.sin(2 * np.pi * f * h * t) / h for h in range(1, 5) if f * h < 10_000)
    buf[start : start + n] += amp * env * tone


def _progression(chords: list[tuple[int, bool]], seconds: float, *, cents: float = 0.0, chord_s: float = 2.0) -> np.ndarray:
    buf = np.zeros(int(seconds * RATE))
    t, i = 0.0, 0
    while t + 1e-9 < seconds:
        pc, minor = chords[i % len(chords)]
        for m in (36 + pc, 48 + pc, 48 + pc + (3 if minor else 4), 48 + pc + 7):
            _note(buf, m, t, min(chord_s, seconds - t), 0.15, cents)
        t += chord_s
        i += 1
    return buf


def _clicks(buf: np.ndarray, bpm: float, amp: float) -> None:
    rng = np.random.default_rng(7)
    period = 60.0 / bpm
    n = int(0.003 * RATE)
    t = np.arange(n) / RATE
    burst = amp * np.exp(-t / 0.001) * (0.6 * np.sin(2 * np.pi * 2500 * t) + 0.4 * rng.uniform(-1, 1, n))
    k = 0
    while (start := round(k * period * RATE)) < buf.size:
        end = min(start + n, buf.size)
        buf[start:end] += burst[: end - start]
        k += 1


def _signal(name: str) -> np.ndarray:
    c_major = [(0, False), (5, False), (7, False), (0, False)]
    a_minor = [(9, True), (2, True), (4, False), (9, True)]
    e_major = [(4, False), (1, True), (9, False), (11, False)]
    f_minor = [(5, True), (10, True), (0, False), (5, True)]
    if name == "c_major_progression":
        buf = _progression(c_major, 24.0)
    elif name == "a_minor_progression":
        buf = _progression(a_minor, 24.0)
    elif name == "e_major_loop_with_clicks":
        buf = _progression(e_major, 32.0)
        _clicks(buf, 124.0, 0.3)
    elif name == "f_minor_detuned_plus_8_cents":
        buf = _progression(f_minor, 24.0, cents=8.0)
    elif name == "fast_chords_bb_major":
        buf = _progression([(10, False), (3, False), (5, False), (7, True)], 20.0, chord_s=0.5)
    else:
        raise ValueError(name)
    return 0.5 * buf / np.max(np.abs(buf))


def _write_wav(path: Path, mono: np.ndarray) -> None:
    pcm = np.round(np.clip(mono, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


SIGNALS = [
    "c_major_progression",
    "a_minor_progression",
    "e_major_loop_with_clicks",
    "f_minor_detuned_plus_8_cents",
    "fast_chords_bb_major",
]


@pytest.mark.parametrize("name", SIGNALS)
def test_mean_chroma_matches_librosa_and_key_matches_profiles(
    name: str, tmp_path: Path, odj_live: Path, librosa_module: ModuleType
) -> None:
    path = tmp_path / f"{name}.wav"
    _write_wav(path, _signal(name))

    run = subprocess.run([str(odj_live), "--file", str(path), "--summary"], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert len(lines) == 1, lines[:3]
    summary = json.loads(lines[0])
    ours = np.asarray(summary["chroma_mean"], dtype=np.float64)
    assert ours.shape == (12,) and ours.max() > 0, summary

    y, sr = librosa_module.load(str(path), sr=22050, mono=True)
    reference = librosa_module.feature.chroma_cqt(y=y, sr=sr)
    theirs = reference.mean(axis=1)
    cosine = float(ours @ theirs / (np.linalg.norm(ours) * np.linalg.norm(theirs)))
    estimate = profiles.estimate_key_krumhansl(reference)
    want = canon.to_camelot(estimate.key)
    print(
        f"PARITY {name}: cosine={cosine:.4f} ours={summary['key']} profiles_on_librosa={want} "
        f"librosa={librosa_module.__version__} frames ours={summary['analysis_frames']} librosa={reference.shape[1]}"
    )
    assert cosine >= COSINE_FLOOR, f"{name}: mean chroma cosine {cosine:.4f} < {COSINE_FLOOR}"
    assert summary["key"] == want, f"{name}: odj-live says {summary['key']}, profiles.py on librosa chroma says {want}"
