"""Shared real-audio stem-sets for the publisher quality gate tests (#9664).

Parts come from the real encoders (``apps.stems.stem_encode``), so the
publisher's gate measures the same bytes production would hand it. Not a
``test_`` module; its users mark themselves ``requires_audio_stack``.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    import numpy as np
    import soundfile as sf
except ImportError:
    # Users mark themselves requires_audio_stack, which tests/conftest.py turns
    # into an UNAVAILABLE report; never a skip here.
    np = sf = None  # type: ignore[assignment]

from apps.stems.stem_encode import encode_flac_bytes, encode_mp3, encode_part
from apps.stems.stem_size_policy import effective_bitrate_kbps

RATE = 44100
SECONDS = 4.0
FRAMES = int(RATE * SECONDS)
PARTS = ("vocals", "drums", "bass", "other")


def noise(seed: int) -> Any:
    rng = np.random.default_rng(seed)
    return (0.3 * rng.uniform(-1.0, 1.0, size=(FRAMES, 2))).astype(np.float32)


def mp3_source(path: Path, *, kbps: int, seed: int = 0) -> Path:
    """A real CBR MP3 'track' at ``kbps``, the source the parts are judged against."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encode_mp3(noise(seed), RATE, path, cbr_kbps=kbps)
    return path


def compliant_mp3_parts(source: Path, *, seed: int, tmp: Path) -> dict[str, bytes]:
    """The by-source producer path: encode_part at the whole-set rate with its step-down."""
    tmp.mkdir(parents=True, exist_ok=True)
    source_bytes = source.stat().st_size
    source_kbps = effective_bitrate_kbps(source_bytes, SECONDS)
    return {
        part: encode_part(
            noise(seed * 10 + i),
            RATE,
            codec="mp3",
            part=part,
            mp3_settings=None,
            source_bytes=source_bytes,
            source_kbps=source_kbps,
            out_path=tmp / f"{part}.mp3",
        )[0]
        for i, part in enumerate(PARTS)
    }


def mp3_parts_at(kbps_by_part: Mapping[str, int], *, seed: int, tmp: Path) -> dict[str, bytes]:
    """Parts forced to the given CBR rates, for the breaking sets."""
    tmp.mkdir(parents=True, exist_ok=True)
    return {
        part: encode_mp3(noise(seed * 10 + i), RATE, tmp / f"{part}.mp3", cbr_kbps=kbps_by_part[part])
        for i, part in enumerate(PARTS)
    }


def lossless_parts(codec: str, *, seed: int) -> dict[str, bytes]:
    """FLAC or WAV PCM_16 parts, the explicit control-arm shape."""
    if codec == "flac":
        return {part: encode_flac_bytes(noise(seed * 10 + i), RATE) for i, part in enumerate(PARTS)}
    if codec == "wav":
        out: dict[str, bytes] = {}
        for i, part in enumerate(PARTS):
            buffer = io.BytesIO()
            sf.write(buffer, noise(seed * 10 + i), RATE, format="WAV", subtype="PCM_16")
            out[part] = buffer.getvalue()
        return out
    raise ValueError(f"lossless_parts takes flac or wav, got {codec!r}")


def publish_result(parts: Mapping[str, bytes], codec: str) -> dict[str, Any]:
    """The separator result shape both publishers take."""
    audio: dict[str, Any] = {"sample_rate": RATE, "frame_count": FRAMES, "channels": 2, "codec": codec}
    if codec in {"flac", "wav"}:
        audio["bit_depth"] = 16
    return {
        "source_sha256": "0" * 64,
        "stem_ext": codec,
        "stem_codec": codec,
        "size_policy": None,
        "audio": audio,
        "stems": dict(parts),
    }
