"""Serato adapter capability descriptor (open-dj §6 + §7).

Declares which open-dj fields the Serato adapter supports losslessly, which
it downgrades, and which it drops. Matches the decisions in CONTEXT.md D6:

  * ``rating``: lossy (Serato has no native rating field).
  * ``memory_cue``: lossy (Serato has no memory-cue concept; drop-with-warning
    default, opt-in promotion to hot cue via ``memory_as_hot``).
  * ``beats.variable_tempo``: lossless (Serato tracks every anchor).
  * Everything else listed here is lossless for the v0 schema.

The descriptor is pure data -- snapshot tests compare its JCS bytes against
``tests/data/serato_capabilities.snapshot.json``.
"""

from __future__ import annotations

from apps.open_dj import Capabilities, CapabilityField


SERATO_CAPABILITIES = Capabilities(
    adapter="serato",
    version="0.1.0",
    fields=(
        CapabilityField(field="track_id", supports="lossless", rationale="computed from file path + tsng"),
        CapabilityField(field="file_path", supports="lossless"),
        CapabilityField(field="title", supports="lossless"),
        CapabilityField(field="artists", supports="lossy", rationale="Serato stores a single artist string; artists joined on ' & '."),
        CapabilityField(field="album", supports="lossless"),
        CapabilityField(field="bpm", supports="lossless"),
        CapabilityField(field="key_camelot", supports="lossless", rationale="Serato key string round-trips through the Camelot map."),
        CapabilityField(field="rating", supports="unsupported", rationale="Serato has no native rating field; drop-with-warning (D6)."),
        CapabilityField(field="cue_points.hot", supports="lossless"),
        CapabilityField(field="cue_points.memory", supports="unsupported", rationale="Serato has no memory cues; drop-with-warning (D6)."),
        CapabilityField(field="cue_points.loop", supports="lossless"),
        CapabilityField(field="cue_points.color", supports="lossless", rationale="24-bit RGB round-trips."),
        CapabilityField(field="beats", supports="lossless"),
        CapabilityField(field="color_rgb", supports="lossless"),
        CapabilityField(field="play_count", supports="lossless"),
        CapabilityField(field="isrc", supports="lossy", rationale="Stored in a custom Serato column; use x_serato_isrc extension."),
        CapabilityField(field="smart_crates", supports="unsupported", rationale="Treated as opaque x_serato_smart_crate extension."),
    ),
)


def capabilities() -> Capabilities:
    """Return a snapshot of the Serato adapter capability descriptor."""
    return SERATO_CAPABILITIES
