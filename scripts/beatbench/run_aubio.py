#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["aubio", "numpy<2", "soundfile"]
# ///
"""Candidate (b): aubio's onset-driven tempo tracker, the second baseline.

WHY A SECOND BASELINE. librosa and aubio fail differently, and knowing which
kind of failure a rekordbox grid punishes is more useful than knowing that some
generic baseline is worse than a neural model. aubio tracks tempo causally,
frame by frame, with no global prior, so unlike librosa it CAN follow a tempo
that moves. What it gives up for that is stability: a causal tracker has no
opportunity to revise an early mistake once the track continues.

BUILDING THIS ON ARM64 MACOS NEEDS TWO WORKAROUNDS, both recorded here because
they are non-obvious and cost real time to rediscover. aubio 0.4.9 is from 2019
and its sole wheel-less source build breaks twice on a current toolchain:

  1. clang 16 promoted incompatible function pointer types from warning to
     error, which kills python/ext/ufuncs.c. Fixed by
     CFLAGS=-Wno-incompatible-function-pointer-types.
  2. aubio's optional ffmpeg audio input targets the ffmpeg 4 API, and a
     current brew ffmpeg no longer has it, so src/io/source_avcodec.c fails.
     Fixed by pointing PKG_CONFIG_LIBDIR at nothing, so the build finds no
     ffmpeg and compiles without codec support.

Losing aubio's own file reading costs nothing here: the fixtures are already
decoded WAVs and the audio is handed over as a numpy array, so only aubio's
tempo core is used. The justfile recipe sets both variables.

NO DOWNBEATS. aubio has no downbeat estimator, so this reports None.
"""

from __future__ import annotations

import sys

import _harness
import aubio
import numpy as np
import soundfile as sf

# aubio's default tempo settings. The hop size sets the resolution of a beat
# time: 512 frames at 44.1 kHz is roughly 11.6 ms, comfortably finer than the
# 70 ms agreement tolerance, so the hop is not the limiting factor.
WIN_SIZE = 1024
HOP_SIZE = 512


def build_analyzer() -> _harness.Analyzer:
    def analyze(wav_path: str) -> tuple[list[float], None, float | None]:
        audio, sr = sf.read(wav_path, dtype="float32", always_2d=False)
        if audio.ndim > 1:
            audio = audio.mean(axis=1).astype(np.float32)

        tracker = aubio.tempo("default", WIN_SIZE, HOP_SIZE, sr)
        beats: list[float] = []
        for start in range(0, len(audio) - HOP_SIZE + 1, HOP_SIZE):
            block = audio[start : start + HOP_SIZE]
            if tracker(block)[0]:
                beats.append(float(tracker.get_last_s()))

        # aubio exposes a running tempo estimate; fall back to the beat spacing
        # when it has not settled, rather than reporting a tempo it never gave.
        native_bpm = float(tracker.get_bpm()) or None
        return beats, None, native_bpm

    return analyze


if __name__ == "__main__":
    args = _harness.build_argparser(__doc__, default_workers=6).parse_args()
    sys.exit(
        _harness.run(
            args,
            candidate="aubio",
            version=aubio.version,
            license_note="GPL-3.0 (code), no model weights",
            shippable=False,
            emits_downbeats=False,
            build_analyzer=build_analyzer,
        )
    )
