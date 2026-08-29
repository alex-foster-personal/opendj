#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["librosa>=0.11", "numpy<2", "soundfile"]
# ///
"""Candidate (a): the librosa beat tracker, as the floor of the comparison.

THIS IS THE BASELINE EVERYTHING ELSE HAS TO BEAT. librosa's beat_track is the
default answer a Python audio project reaches for, it is ISC licensed and
shippable without qualification, and it has no model weights to acquire. If a
heavier candidate cannot clearly beat it, that candidate has not earned its
dependency footprint.

librosa's tracker is a dynamic-programming beat tracker over an onset envelope
with a single global tempo prior. That design is the reason it is here twice
over: it is the sensible baseline AND it is the clearest example of the failure
this benchmark is built to expose. A single global tempo cannot by construction
follow a rekordbox dynamic grid, so the fixed-versus-dynamic split should show
it degrading where a tempo-tracking model does not.

NO DOWNBEATS. librosa has no downbeat estimator, so downbeats are reported as
None and the scorer records N/A. Synthesising a downbeat by assuming every
fourth beat starts a bar would be inventing an answer.
"""

from __future__ import annotations

import sys

import _harness
import librosa
import numpy as np


def build_analyzer() -> _harness.Analyzer:
    def analyze(wav_path: str) -> tuple[list[float], None, float | None]:
        # Fixtures are already mono at a known rate; sr=None keeps the decoded
        # rate rather than silently resampling to librosa's 22050 default.
        audio, sr = librosa.load(wav_path, sr=None, mono=True)
        tempo, frames = librosa.beat.beat_track(y=audio, sr=sr, units="frames")
        beats = librosa.frames_to_time(frames, sr=sr)
        native_bpm = float(np.atleast_1d(tempo)[0]) if tempo is not None else None
        return [float(b) for b in beats], None, native_bpm

    return analyze


if __name__ == "__main__":
    args = _harness.build_argparser(__doc__, default_workers=6).parse_args()
    sys.exit(
        _harness.run(
            args,
            candidate="librosa",
            version=librosa.__version__,
            license_note="ISC (code), no model weights",
            shippable=True,
            emits_downbeats=False,
            build_analyzer=build_analyzer,
        )
    )
