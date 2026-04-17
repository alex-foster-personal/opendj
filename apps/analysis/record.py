"""Canonical :class:`AnalysisRecord` dataclass + JSON (de)serialisation.

Schema follows 06-CONTEXT §D2 verbatim.  Round-trip stable through
:meth:`to_json` / :meth:`from_json`; datetimes normalise to trailing ``Z``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

EnergySource = Literal["mik", "inferred"]


@dataclass(frozen=True)
class AnalysisRecord:
    """One backend's analysis result for one track."""

    stable_id: str
    backend: str
    backend_version: str
    analyzed_at: datetime

    duration_s: float
    sample_rate: int

    bpm: float
    bpm_confidence: float

    key_camelot: str
    key_openkey: str
    key_confidence: float

    energy: int
    energy_source: EnergySource = "inferred"

    onsets_s: list[float] = field(default_factory=list)
    downbeats_s: list[float] = field(default_factory=list)
    rms_peaks_s: list[float] = field(default_factory=list)

    features_blob: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        data = asdict(self)
        data["analyzed_at"] = _dt_to_iso(self.analyzed_at)
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "AnalysisRecord":
        data = json.loads(raw)
        data["analyzed_at"] = _iso_to_dt(data["analyzed_at"])
        for k in ("onsets_s", "downbeats_s", "rms_peaks_s"):
            data.setdefault(k, [])
        data.setdefault("features_blob", {})
        return cls(**data)


def _dt_to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _iso_to_dt(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


__all__ = ["AnalysisRecord", "EnergySource"]
