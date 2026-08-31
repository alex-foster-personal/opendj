#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["essentia", "numpy<2"]
# ///
"""Candidate (c): Essentia RhythmExtractor2013, the strongest classical baseline.

WHY THIS ONE OF ESSENTIA'S SEVERAL TRACKERS. RhythmExtractor2013 in
"multifeature" mode is the accuracy-oriented option: it runs several beat
trackers over different onset features and takes the consensus, which is
consistently the best-scoring non-neural approach in the MIREX-lineage
evaluations. The "degara" mode is faster and weaker; picking the faster one and
then reporting that classical methods lose would be rigging the comparison.

IT WAS EXPECTED TO BE UNINSTALLABLE AND IS NOT. The brief anticipated that
Essentia might not build on an arm64 Mac, which was true for years. As of this
round a working arm64 wheel resolves and imports cleanly under Python 3.11, so
no skip is needed and the fallback plan is unused. Recorded because it reverses
a standing assumption.

LICENCE IS THE REASON THIS MAY NOT BE SHIPPABLE. Essentia is AGPL-3.0. That is
strong copyleft with a network clause, which is a materially different
proposition from librosa's ISC. It is benchmarked because it sets the classical
ceiling, but treat adoption as a licensing decision and not merely a technical
one.

NO DOWNBEATS. RhythmExtractor2013 returns beats, a global BPM, a confidence and
a tempo histogram, but no bar positions, so downbeats are reported as None.
"""

from __future__ import annotations

import sys

import _harness
import essentia
from essentia.standard import MonoLoader, RhythmExtractor2013

# Essentia is chatty on stderr about perfectly normal conditions; the harness
# already reports real failures, so silence the informational stream.
essentia.log.infoActive = False
essentia.log.warningActive = False

SAMPLE_RATE = 44100


def build_analyzer() -> _harness.Analyzer:
    extractor = RhythmExtractor2013(method="multifeature")

    def analyze(wav_path: str) -> tuple[list[float], None, float | None]:
        audio = MonoLoader(filename=wav_path, sampleRate=SAMPLE_RATE)()
        bpm, beats, _confidence, _estimates, _intervals = extractor(audio)
        return [float(b) for b in beats], None, float(bpm)

    return analyze


if __name__ == "__main__":
    args = _harness.build_argparser(__doc__, default_workers=6).parse_args()
    sys.exit(
        _harness.run(
            args,
            candidate="essentia",
            version=essentia.__version__,
            license_note="AGPL-3.0 (code); commercial licence needed otherwise",
            shippable=False,
            emits_downbeats=False,
            build_analyzer=build_analyzer,
        )
    )
