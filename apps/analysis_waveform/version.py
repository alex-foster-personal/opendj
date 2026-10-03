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

WHAT 1.2.0 IS. The same peaks; the 1200-column preview is now encoded on
rekordbox PWV6's per-band scale (`bands.pwv6_scale`), so it draws with the
balance JIK approved (specs/ui-contracts/library-preview-waveform). MINOR,
because emitted preview values change. The detail lane is unchanged. 1.1.0
is skipped here on purpose: on main-electron-rust it names the Rust engine
decoder (NATIVE-21), which this branch does not carry.

WHAT 1.4.0 IS. The same peaks; the preview's per-band gains are re-fitted on
real music (`bands.PWV6_MUSIC_GAIN`, 83/56/176) because the sine-derived ones
drew own rows with no blue lows at all. MINOR: emitted preview values change.
1.3.0 is the engine decoder ported from main-electron-rust (#5013); this
number sits above it whichever of the two lands first.

-Cursor
"""
from __future__ import annotations

from typing import Literal

PRODUCER_VERSION = "1.4.0"

#: The producer half of the `own_<lane>.<producer>` backend name.
PRODUCER: Literal["backfill"] = "backfill"

#: Selection lane this package produces.
LANE: Literal["waveform"] = "waveform"

__all__ = ["LANE", "PRODUCER", "PRODUCER_VERSION"]
