#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#   "madmom @ git+https://github.com/CPJKU/madmom",
#   "numpy<2", "scipy", "cython", "mido",
# ]
# ///
"""Candidate (e): madmom, REFERENCE ONLY. This can never ship. Read this first.

THE LICENCE IS THE ENTIRE POINT OF THIS FILE'S NAME. madmom is split-licensed
and the split is easy to miss:

  - source code: BSD 3-clause, fine
  - the trained models in madmom/models: Creative Commons
    Attribution-NonCommercial-ShareAlike 4.0

Every beat and downbeat processor here loads those models. NonCommercial means
they cannot be used in a product, and ShareAlike means anything built on them
inherits the same terms. So madmom's numbers exist in this benchmark for one
reason only: to show how far a shippable candidate is from the accuracy the
field can reach when licensing is ignored. Every row it produces must be
labelled "madmom (reference, non-shippable)" in any table, and this dependency
must never be added to requirements.txt, pyproject.toml, or any shared
environment. It lives in this file's own PEP 723 block and nowhere else.

THIS ALREADY WENT WRONG ONCE IN THIS REPO, which is why the warning is this
loud. requirements.txt line 27 currently describes madmom as MIT and pulls it
into the dev environment, and apps/analysis/backends/librosa_madmom.py is a
SHIPPED backend calling RNNBeatProcessor and RNNDownBeatProcessor. Both of
those load CC BY-NC-SA weights. That is a pre-existing exposure this round
found rather than created, and it is reported upward rather than fixed here,
because it is a licensing decision and not a benchmark change.

WHY IT IS STILL WORTH MEASURING. madmom's DBN downbeat tracker was the field
reference for years, and it is the thing beat_this was built to beat. Without
it in the table, a reader cannot tell whether beat_this is genuinely good or
merely better than the classical baselines.

DOWNBEATS COME FREE. The downbeat processor emits (time, beat-in-bar) pairs,
so beat positions and bar positions come from one pass, and beat-in-bar 1 is
the downbeat -- the same convention rekordbox's PQTZ uses.
"""

from __future__ import annotations

import sys

import _harness
import madmom
from madmom.features.downbeats import DBNDownBeatTrackingProcessor, RNNDownBeatProcessor

# 3 and 4 cover almost everything in a DJ library; offering more meters would
# let the tracker explain a wrong grid by inventing an exotic time signature.
BEATS_PER_BAR = [3, 4]
FPS = 100


def build_analyzer() -> _harness.Analyzer:
    rnn = RNNDownBeatProcessor()
    dbn = DBNDownBeatTrackingProcessor(beats_per_bar=BEATS_PER_BAR, fps=FPS)

    def analyze(wav_path: str) -> tuple[list[float], list[float], None]:
        activations = rnn(wav_path)
        tracked = dbn(activations)
        beats = [float(row[0]) for row in tracked]
        downbeats = [float(row[0]) for row in tracked if int(row[1]) == 1]
        return beats, downbeats, None

    return analyze


if __name__ == "__main__":
    args = _harness.build_argparser(__doc__, default_workers=6).parse_args()
    sys.exit(
        _harness.run(
            args,
            candidate="madmom (reference, non-shippable)",
            version=f"{madmom.__version__} (models CC BY-NC-SA 4.0)",
            license_note="BSD-3 code, models CC BY-NC-SA 4.0 NON-COMMERCIAL: never ship",
            shippable=False,
            emits_downbeats=True,
            build_analyzer=build_analyzer,
        )
    )
