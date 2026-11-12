"""RTMP sink configuration and key redaction (STREAM-11743)."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_VIDEO_SIZE = "1280x720"
DEFAULT_AUDIO_KBPS = 160
MIN_AUDIO_KBPS = 32
MAX_AUDIO_KBPS = 320
RTMP_SCHEMES = ("rtmp://", "rtmps://")
KEY_PLACEHOLDER = "<key>"
_VIDEO_SIZE_RE = re.compile(r"^(\d{2,4})x(\d{2,4})$")


class RtmpConfigRefused(ValueError):
    """Invalid RTMP configuration; messages never include the stream key."""


@dataclass(frozen=True)
class RtmpConfig:
    url: str
    key: str
    image: Path | None
    video_size: str
    audio_kbps: int

    def __repr__(self) -> str:
        return (
            f"RtmpConfig(url={self.url!r}, key=<redacted>, image={self.image!r}, "
            f"video_size={self.video_size!r}, audio_kbps={self.audio_kbps})"
        )

    @property
    def target(self) -> str:
        return self.url.rstrip("/") + "/" + self.key

    @property
    def redacted_target(self) -> str:
        return self.url.rstrip("/") + "/" + KEY_PLACEHOLDER

    def width_height(self) -> tuple[int, int]:
        w, h = _parse_video_size(self.video_size)
        return w, h

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "key": KEY_PLACEHOLDER,
            "key_set": True,
            "image": None if self.image is None else str(self.image),
            "video_size": self.video_size,
            "audio_kbps": self.audio_kbps,
            "target": self.redacted_target,
        }


def _parse_video_size(size: str) -> tuple[int, int]:
    match = _VIDEO_SIZE_RE.match(size)
    if not match:
        raise RtmpConfigRefused(f"video_size {size!r} must look like 1280x720 with even width and height")
    w, h = int(match.group(1)), int(match.group(2))
    if w % 2 or h % 2:
        raise RtmpConfigRefused(f"video_size {size!r} needs even width and height for yuv420p")
    return w, h


def redact(text: str, key: str) -> str:
    if not key:
        return text
    out = text.replace(key, KEY_PLACEHOLDER)
    quoted = urllib.parse.quote(key, safe="")
    if quoted != key:
        out = out.replace(quoted, KEY_PLACEHOLDER)
    return out


def parse_rtmp_config(
    *,
    url: str,
    key: str,
    image: str | Path | None,
    video_size: str = DEFAULT_VIDEO_SIZE,
    audio_kbps: int = DEFAULT_AUDIO_KBPS,
) -> RtmpConfig:
    if not any(url.startswith(s) for s in RTMP_SCHEMES) or any(ch.isspace() for ch in url):
        raise RtmpConfigRefused(f"url {url!r} must start with rtmp:// or rtmps:// and contain no whitespace")
    if not key or any(ch.isspace() for ch in key) or "/" in key or key == KEY_PLACEHOLDER:
        raise RtmpConfigRefused("stream key must be non-empty, contain no whitespace or slashes")
    image_path: Path | None
    if image is None:
        image_path = None
    else:
        image_path = Path(image).expanduser()
        if not image_path.is_file():
            raise RtmpConfigRefused(f"image {image_path} does not exist or is not a file")
    _parse_video_size(video_size)
    if isinstance(audio_kbps, bool) or not isinstance(audio_kbps, int):
        raise RtmpConfigRefused(f"audio_kbps {audio_kbps!r} is not a whole number of kbps")
    if not MIN_AUDIO_KBPS <= audio_kbps <= MAX_AUDIO_KBPS:
        raise RtmpConfigRefused(f"audio_kbps {audio_kbps} is outside {MIN_AUDIO_KBPS} to {MAX_AUDIO_KBPS}")
    return RtmpConfig(url=url, key=key, image=image_path, video_size=video_size, audio_kbps=audio_kbps)


__all__ = [
    "DEFAULT_AUDIO_KBPS",
    "DEFAULT_VIDEO_SIZE",
    "KEY_PLACEHOLDER",
    "MAX_AUDIO_KBPS",
    "MIN_AUDIO_KBPS",
    "RTMP_SCHEMES",
    "RtmpConfig",
    "RtmpConfigRefused",
    "parse_rtmp_config",
    "redact",
]
