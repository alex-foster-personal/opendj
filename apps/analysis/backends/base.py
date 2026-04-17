"""Analyser backend protocol + shared exceptions."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..record import AnalysisRecord


class BackendNotAvailable(RuntimeError):
    """Backend runtime dep missing (e.g. ``mixed-in-key-cli`` not on PATH)."""


class TrackTooLong(RuntimeError):
    """Track exceeded ``analyzer.max_track_minutes``."""


@runtime_checkable
class AnalyzerBackend(Protocol):
    name: str
    version: str

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        """Pure compute; must not write to disk or state layer."""
        ...  # pragma: no cover


__all__ = ["AnalyzerBackend", "BackendNotAvailable", "TrackTooLong"]
