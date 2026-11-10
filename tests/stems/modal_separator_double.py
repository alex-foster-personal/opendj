"""TEST DOUBLE for the Modal call in ``scripts/stems_modal_worker.py``.

THIS IS TEST CODE AND IS NEVER IMPORTED BY THE APP. The worker reaches it
only when ``MDT_STEMS_SEPARATOR`` names it, which no real run sets. It exists
so the job pipeline -- enqueue, spawn, progress protocol, bundle write,
``library.changed``, UI -- can be exercised end to end without buying GPU
time, while the ONLY substituted step is the vendor call itself.

What it does NOT do is fabricate stems. It encodes real audio from the real
source file with ffmpeg, in the codec production would write for the tier:
MP3 CBR at each part's whole-set rate for a by-source tier (STEM-8231, so the
publisher's quality gate accepts it, #9664), FLAC for a tier that asks for
FLAC by name. Every byte on disk came from audio that exists,
and the worker stamps the manifest's model as ``test-double:...`` so a bundle
produced this way can never be mistaken for a separation. Four identical
parts is not a separation and is not claimed to be one; what is under test is
the pipeline around the separation, not the separation.

-Claude
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

# Short on purpose: the pipeline does not care how long the audio is, and a
# 2-second encode keeps a pipeline test in the seconds, not the minutes.
CLIP_SECONDS: float = 2.0
STEM_PARTS: tuple[str, ...] = ("vocals", "drums", "bass", "other")


def separate(
    tracks: Sequence[Any], tier_key: str
) -> Iterator[dict[str, Any]]:
    """Yield one result per track, in the worker's expected result shape."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError(
            "the stems separator double needs ffmpeg and ffprobe on PATH; it "
            "encodes real audio rather than writing placeholder bytes"
        )
    for track in tracks:
        yield _result_for(track, tier_key)


def _result_for(track: Any, tier_key: str) -> dict[str, Any]:
    from apps.stems.bundle_publish import route_stem_codec

    source = Path(track.audio_path)
    codec = "flac" if route_stem_codec(tier_key) == "flac" else "mp3"
    with tempfile.TemporaryDirectory(prefix="stems-double-") as tmp:
        clip = Path(tmp) / "clip.flac"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-t", str(CLIP_SECONDS),
                "-ac", "2", "-ar", "44100", "-sample_fmt", "s16",
                str(clip),
            ],
            check=True,
        )
        probed = _probe(clip)
        if codec == "flac":
            payload = clip.read_bytes()
            stems = {part: payload for part in STEM_PARTS}
        elif codec == "mp3":
            stems = {part: _mp3_part(clip, source, part, Path(tmp)) for part in STEM_PARTS}
            probed = {k: v for k, v in probed.items() if k != "bit_depth"} | {"codec": "mp3"}

    return {
        "stable_id": track.stable_id,
        # The same real clip in every part. Honest because the worker stamps
        # the manifest as a test double; see that stamp before believing any
        # bundle written this way.
        "stems": stems,
        "stem_ext": codec,
        "stem_codec": codec,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "audio": probed,
        "container_s": 0.0,
    }


def _mp3_part(clip: Path, source: Path, part: str, tmp: Path) -> bytes:
    """The clip as LAME CBR at max(whole-set rate, part floor), production's rate."""
    from apps.stems.stem_size_policy import (
        effective_bitrate_kbps,
        mp3_part_floor_kbps,
        mp3_whole_set_cbr_kbps,
    )

    source_kbps = effective_bitrate_kbps(source.stat().st_size, _duration_s(source))
    kbps = max(mp3_whole_set_cbr_kbps(source_kbps), mp3_part_floor_kbps(part))
    out = tmp / f"{part}.mp3"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(clip), "-c:a", "libmp3lame", "-b:a", f"{kbps}k", str(out),
        ],
        check=True,
    )
    return out.read_bytes()


def _duration_s(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def _probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels,duration_ts",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(out.stdout)["streams"][0]
    sample_rate = int(stream["sample_rate"])
    frame_count = int(stream.get("duration_ts") or 0) or int(
        sample_rate * CLIP_SECONDS
    )
    return {
        "sample_rate": sample_rate,
        "channels": int(stream["channels"]),
        "frame_count": frame_count,
        "bit_depth": 16,
        "codec": "flac",
    }


__all__ = ["CLIP_SECONDS", "STEM_PARTS", "separate"]
