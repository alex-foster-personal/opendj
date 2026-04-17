"""Traktor adapter capability descriptor."""

from __future__ import annotations

from apps.open_dj import Capabilities, CapabilityField


TRAKTOR_CAPABILITIES = Capabilities(
    adapter="traktor",
    version="0.1.0",
    fields=(
        CapabilityField(field="track_id", supports="lossless", rationale="computed from LOCATION path"),
        CapabilityField(field="file_path", supports="lossless"),
        CapabilityField(field="title", supports="lossless"),
        CapabilityField(field="artists", supports="lossy", rationale="Traktor stores a single string; artists joined on ' & '."),
        CapabilityField(field="album", supports="lossless"),
        CapabilityField(field="bpm", supports="lossless"),
        CapabilityField(field="key_camelot", supports="lossless", rationale="Mapped through Camelot <-> open-key 0..23 table."),
        CapabilityField(field="rating", supports="lossless", rationale="Stored as INFO @RANKING in steps of 51."),
        CapabilityField(field="cue_points.hot", supports="lossless"),
        CapabilityField(field="cue_points.memory", supports="lossy", rationale="Traktor has no memory-cue concept; encoded as hot cues on write."),
        CapabilityField(field="cue_points.loop", supports="lossless"),
        CapabilityField(field="cue_points.color", supports="lossy", rationale="Traktor uses fixed-per-type palette (D6); user colours on write are replaced."),
        CapabilityField(field="beats", supports="lossless"),
        CapabilityField(field="color_rgb", supports="lossy", rationale="Traktor COLOR is a 1..16 palette index, not 24-bit RGB."),
        CapabilityField(field="play_count", supports="lossless"),
        CapabilityField(field="isrc", supports="unsupported", rationale="No native ISRC field; stored in x_traktor_isrc extension."),
        CapabilityField(field="smart_playlists", supports="unsupported", rationale="Opaque SMARTLIST passthrough as x_traktor_smartlist."),
        CapabilityField(field="extended_data", supports="lossless", rationale="EXTENDEDDATA preserved as base64 extension when preserve_extended_data=True."),
    ),
)


def capabilities() -> Capabilities:
    """Return the Traktor adapter capability descriptor."""
    return TRAKTOR_CAPABILITIES
