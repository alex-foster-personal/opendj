#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#   "beat_this @ git+https://github.com/CPJKU/beat_this",
#   "torch", "numpy<2", "soundfile",
# ]
# ///
"""Candidate (d): beat_this (CPJKU), the primary candidate of this benchmark.

WHY THIS IS THE ONE THAT MATTERS. beat_this is the only candidate here that is
both state of the art AND unambiguously shippable: the code and the trained
weights are MIT. That combination is rare in this field. madmom, its closest
peer in accuracy, ships models under a non-commercial licence and therefore can
never be more than a reference point. So the question this benchmark exists to
answer is narrow: does beat_this agree with rekordbox closely enough to be
worth building on, and how does it behave where librosa cannot follow.

WHAT IT IS. A transformer over a log-mel spectrogram that predicts beat and
downbeat activations jointly, trained with a loss that tolerates small
annotation shifts. It emits downbeats natively, which most of the field's
lighter options do not, so it is one of only two candidates here that can be
scored on bar-1 agreement at all.

DBN POSTPROCESSING IS DELIBERATELY OFF. beat_this can optionally route its
activations through madmom's DBN, which would both slow it down and drag the
non-commercial dependency back in through the side door. The whole point of
preferring beat_this is that it does not need madmom, so it is measured without
it. Its own minimal postprocessing is what a shipped integration would use.

CPU IS THE HONEST DEVICE FOR ROUND 0. The target is a laptop doing library
analysis in the background, not a GPU box, so CPU timing is the number that
decides whether a full-library pass is practical. If the realtime factor makes
a 10k pass unreasonable, that is a finding, not a reason to quietly switch
devices and report a number the user will never see.

WEIGHTS ARE FETCHED ONCE on first use into the torch hub cache. That download
happens inside the measured load phase and is reported separately from
inference, so it does not contaminate the per-track cost.
"""

from __future__ import annotations

import sys

import _harness
import torch
from beat_this.inference import File2Beats

# "final0" is the released single-model checkpoint. The ensemble would be
# slower for a round-0 baseline and would blur which model is being measured.
CHECKPOINT = "final0"


def build_analyzer() -> _harness.Analyzer:
    # Threads are capped per worker because the harness already runs fixtures
    # concurrently; letting torch also fan out oversubscribes the cores and
    # makes the realtime factor look worse than the model actually is.
    torch.set_num_threads(2)
    file2beats = File2Beats(checkpoint_path=CHECKPOINT, device="cpu", dbn=False)

    def analyze(wav_path: str) -> tuple[list[float], list[float], None]:
        beats, downbeats = file2beats(wav_path)
        # No native tempo: the model emits beat positions, and a tempo read off
        # those positions is the scorer's job, uniformly across candidates.
        return [float(b) for b in beats], [float(d) for d in downbeats], None

    return analyze


if __name__ == "__main__":
    args = _harness.build_argparser(__doc__, default_workers=4).parse_args()
    sys.exit(
        _harness.run(
            args,
            candidate="beat_this",
            version=f"git-main (torch {torch.__version__}, cpu, dbn=off)",
            license_note="MIT (code AND weights)",
            shippable=True,
            emits_downbeats=True,
            build_analyzer=build_analyzer,
        )
    )
