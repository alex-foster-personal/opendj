"""In-memory RTMP sink configuration and lifecycle (STREAM-11743)."""

from __future__ import annotations

import threading
from typing import Any

from apps.stream.config import RtmpConfig, parse_rtmp_config
from apps.stream.rtmp import RtmpPush

SINKS: tuple[str, ...] = ("rtmp",)


class StreamSinkUnknown(ValueError):
    """An unknown sink name was requested."""


class StreamConflict(RuntimeError):
    """The stream sink is not ready or is already running."""


class StreamService:
    """Hold RTMP config and the active push, independent of the recorder session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rtmp_config: RtmpConfig | None = None
        self._push: RtmpPush | None = None

    def configure_rtmp(
        self,
        *,
        url: str,
        key: str,
        image: str | None = None,
        video_size: str | None = None,
        audio_kbps: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._push is not None and self._push.status().state == "running":
                raise StreamConflict("cannot change rtmp config while a stream push is running")
        fields: dict[str, Any] = {"url": url, "key": key, "image": image}
        if video_size is not None:
            fields["video_size"] = video_size
        if audio_kbps is not None:
            fields["audio_kbps"] = audio_kbps
        config = parse_rtmp_config(**fields)
        with self._lock:
            if self._push is not None and self._push.status().state == "running":
                raise StreamConflict("cannot change rtmp config while a stream push is running")
            self._rtmp_config = config
        return config.to_public_dict()

    def rtmp_config(self) -> dict[str, Any] | None:
        with self._lock:
            return None if self._rtmp_config is None else self._rtmp_config.to_public_dict()

    def start(self, sink: str, *, sample_rate: int) -> RtmpPush:
        if sink not in SINKS:
            raise StreamSinkUnknown(f"sink {sink!r} is not one of {', '.join(SINKS)}")
        with self._lock:
            if self._rtmp_config is None:
                raise StreamConflict("configure the rtmp sink first: PUT /api/sets/recorder/stream/rtmp")
            if self._push is not None and self._push.status().state == "running":
                raise StreamConflict("a stream sink is already running")
            config = self._rtmp_config
        push = RtmpPush(config, sample_rate=sample_rate)
        push.start()
        with self._lock:
            if self._push is not None and self._push.status().state == "running":
                push.stop()
                raise StreamConflict("a stream sink is already running")
            self._push = push
        return push

    def status(self) -> dict[str, Any] | None:
        with self._lock:
            if self._push is None:
                return None
            return self._push.status().to_dict()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._push is None:
                raise StreamConflict("no stream sink is running")
            push = self._push
        return push.stop().to_dict()

    def is_running(self) -> bool:
        with self._lock:
            if self._push is None:
                return False
            return self._push.status().state == "running"


__all__ = [
    "SINKS",
    "StreamConflict",
    "StreamService",
    "StreamSinkUnknown",
]
