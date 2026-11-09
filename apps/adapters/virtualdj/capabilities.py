"""VirtualDJ adapter capability descriptor."""

from __future__ import annotations

from apps.open_dj import Capabilities, CapabilityField

VIRTUALDJ_CAPABILITIES = Capabilities(
    adapter="virtualdj",
    version="0.1.0",
    fields=(
        CapabilityField(field="track_id", supports="lossless", rationale="sha256 of the Song FilePath"),
        CapabilityField(
            field="file_path",
            supports="lossless",
            rationale="FilePath kept verbatim, Windows drive paths too",
        ),
        CapabilityField(field="title", supports="lossless"),
        CapabilityField(
            field="artists", supports="lossy", rationale="VirtualDJ stores one Tags Author string"
        ),
        CapabilityField(field="album", supports="lossless"),
        CapabilityField(
            field="bpm",
            supports="lossy",
            rationale="Scan Bpm is seconds per beat; 60 / value is rounded to 3 decimals",
        ),
        CapabilityField(
            field="key_camelot",
            supports="lossy",
            rationale="VirtualDJ key text mapped to Camelot; unrecognized text dropped and counted",
        ),
        CapabilityField(field="rating", supports="lossless", rationale="Tags Stars 0-5"),
        CapabilityField(field="duration_ms", supports="lossless", rationale="Infos SongLength seconds"),
        CapabilityField(field="play_count", supports="lossless"),
        CapabilityField(
            field="cue_points.hot",
            supports="lossy",
            rationale="Poi cue Num 1-8 are pads 0-7; a higher Num reads as a memory cue",
        ),
        CapabilityField(
            field="cue_points.memory",
            supports="lossy",
            rationale="VirtualDJ has no memory cue; a cue Poi off the 8 pads",
        ),
        CapabilityField(
            field="cue_points.loop",
            supports="lossless",
            rationale="Poi loop, Size read as seconds",
        ),
        CapabilityField(field="cue_points.color", supports="unsupported"),
        CapabilityField(
            field="beats",
            supports="lossy",
            rationale="first beatgrid Poi only, at the track BPM",
        ),
        CapabilityField(field="color_rgb", supports="unsupported"),
        CapabilityField(field="isrc", supports="unsupported"),
        CapabilityField(
            field="smart_playlists",
            supports="unsupported",
            rationale="FilterFolder .vdjfolder skipped and counted",
        ),
        CapabilityField(
            field="playlists",
            supports="lossless",
            rationale="MyLists .vdjfolder and .m3u, nested by folder",
        ),
    ),
)


def capabilities() -> Capabilities:
    """Return the VirtualDJ adapter capability descriptor."""
    return VIRTUALDJ_CAPABILITIES
