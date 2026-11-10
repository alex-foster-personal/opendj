"""Mixxx adapter capability descriptor."""

from __future__ import annotations

from apps.open_dj import Capabilities, CapabilityField

MIXXX_CAPABILITIES = Capabilities(
    adapter="mixxx",
    version="0.1.0",
    fields=(
        CapabilityField(field="track_id", supports="lossless", rationale="library.id, kept as the vendor id"),
        CapabilityField(field="file_path", supports="lossless", rationale="track_locations.location (absolute)"),
        CapabilityField(field="title", supports="lossless"),
        CapabilityField(field="album", supports="lossless"),
        CapabilityField(field="bpm", supports="lossless"),
        CapabilityField(
            field="artists",
            supports="lossy",
            rationale="one artist string, not split",
        ),
        CapabilityField(
            field="key_camelot",
            supports="lossy",
            rationale="Mixxx key text (Lancelot, Open Key or traditional) mapped through one table; unknown text dropped and counted",
        ),
        CapabilityField(field="rating", supports="lossless", rationale="0-5 stars"),
        CapabilityField(field="duration_ms", supports="lossless", rationale="seconds x 1000"),
        CapabilityField(field="play_count", supports="lossless", rationale="timesplayed"),
        CapabilityField(field="color_rgb", supports="lossless", rationale="24-bit RGB (Mixxx 2.3+)"),
        CapabilityField(field="cue_points.hot", supports="lossless", rationale="type 1, pad = hotcue"),
        CapabilityField(
            field="cue_points.loop", supports="lossless", rationale="type 4, length converted from samples"
        ),
        CapabilityField(field="cue_points.load", supports="lossless", rationale="type 2 main cue"),
        CapabilityField(field="cue_points.memory", supports="unsupported", rationale="Mixxx has no memory cues"),
        CapabilityField(field="cue_points.color", supports="lossless", rationale="24-bit RGB"),
        CapabilityField(
            field="cue_points.other",
            supports="unsupported",
            rationale="intro, outro, jump, beat, N60dB markers dropped and counted",
        ),
        CapabilityField(field="beats", supports="unsupported", rationale="beatgrid blob not read"),
        CapabilityField(
            field="genre_year",
            supports="lossy",
            rationale="stored as x_mixxx_genre / x_mixxx_year extensions",
        ),
        CapabilityField(
            field="playlists",
            supports="lossless",
            rationale="PlaylistTracks.position order; hidden Auto DJ and set-log playlists skipped",
        ),
        CapabilityField(
            field="crates",
            supports="lossy",
            rationale="unordered in Mixxx; imported as playlists under a Crates folder",
        ),
        CapabilityField(field="write", supports="unsupported", rationale="read-only reader"),
    ),
)


def capabilities() -> Capabilities:
    """Return the Mixxx adapter capability descriptor."""
    return MIXXX_CAPABILITIES
