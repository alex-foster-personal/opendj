"""The waveform lane's producer semver, and the one place it is written down.

`apps/analysis/record.py` makes `producer_version` a required field of every own
record and `apps/analysis/lanes.py` ranks the canonical pointer by it, so this
lane needs a version an ordering function can read. Bump the PATCH for a fix
that cannot change any emitted peaks, the MINOR for a policy change that can (a
new crossover, filter order, column density), and the MAJOR for a change to the
lane payload shape.

WHAT 1.0.0 IS. The shipped ffmpeg tri-band decode (`DecodeProfile` defaults,
`PEAKS_GENERATION` `tri-1`): 200 Hz / 4 kHz crossovers, 44.1 kHz, 150
columns/s detail, 1200-column preview. Model-free: no third-party weights, so
`uses_model=False` and `model_sha256=None` on every record it writes.

WHAT 1.1.0 IS. The same profile, produced by the engine's own decoder
(`odj-audio waveform`, symphonia) when it is available and by ffmpeg
otherwise (`decode.select_decoder`). MINOR, because the engine filters at the
file's own rate: a 48 kHz file's high band keeps its 22-24 kHz content, and
`sample_rate` on the record is now the rate actually filtered at. On 44.1 kHz
FLAC/MP3/WAV the peaks match 1.0.0 to within 1 of 255.

-Cursor
"""
from __future__ import annotations

from typing import Literal

PRODUCER_VERSION = "1.1.0"

#: The producer half of the `own_<lane>.<producer>` backend name.
PRODUCER: Literal["backfill"] = "backfill"

#: Selection lane this package produces.
LANE: Literal["waveform"] = "waveform"

__all__ = ["LANE", "PRODUCER", "PRODUCER_VERSION"]
