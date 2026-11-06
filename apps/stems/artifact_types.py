"""Shared stem-artifact errors and geometry types (keeps parsers free of artifacts.py)."""

from __future__ import annotations

from dataclasses import dataclass


class StemArtifactError(ValueError):
    """An artifact violates the durable precomputed-stems contract."""


@dataclass(frozen=True)
class WavMetadata:
    """WAV fields which must agree across all stem files."""

    sample_rate: int
    frame_count: int
    channels: int
