"""Load local GiantSteps+ key-segment annotations.

Dataset: GiantSteps+ EDM Key Dataset
License: CC BY-SA 4.0
Source: https://zenodo.org/records/4153506
DOI: 10.5281/zenodo.4153506
Attribution: (private path)

This module reads local JSON only. It does not download dataset files.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATASET_LICENSE = "CC BY-SA 4.0"
DATASET_SOURCE_URL = "https://zenodo.org/records/4153506"
DATASET_DOI = "10.5281/zenodo.4153506"
DATASET_NOTICE = "(private path)"


@dataclass(frozen=True)
class KeySegment:
    """One timestamped key segment from a GiantSteps+ annotation."""

    start_s: float
    end_s: float
    key: str


def _field(payload: Mapping[str, Any], primary: str, alias: str) -> Any:
    if primary in payload:
        return payload[primary]
    if alias in payload:
        return payload[alias]
    raise ValueError(f"missing {primary}/{alias} field")


def _array(value: Any, *, field: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be an array")  # noqa: TRY004 - annotation contract
    return list(value)


def load_segments(payload: Mapping[str, Any]) -> tuple[KeySegment, ...]:
    """Parse one GiantSteps+ annotation object into monotonic rows."""
    raw_keys = _field(payload, "key", "key_sequence")
    keys = [raw_keys] if isinstance(raw_keys, str) else _array(raw_keys, field="key")
    starts = _array(_field(payload, "start_times", "start_times_s"), field="start_times")
    ends = _array(_field(payload, "end_times", "end_times_s"), field="end_times")

    if not keys:
        raise ValueError("annotation must contain at least one segment")
    if not (len(keys) == len(starts) == len(ends)):
        raise ValueError("key, start_times, and end_times lengths must match")

    rows: list[KeySegment] = []
    for index, (raw_start, raw_end, raw_key) in enumerate(
        zip(starts, ends, keys, strict=True)
    ):
        try:
            start_s = float(raw_start)
            end_s = float(raw_end)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"segment {index} timestamps must be numbers") from exc
        if not (math.isfinite(start_s) and math.isfinite(end_s)):
            raise ValueError(f"segment {index} timestamps must be finite")
        if not isinstance(raw_key, str) or not raw_key:
            raise ValueError(f"segment {index} key must be a non-empty string")
        if start_s >= end_s:
            raise ValueError(f"segment {index} start_s must be less than end_s")
        if rows and start_s <= rows[-1].start_s:
            raise ValueError("start_times must be strictly increasing")
        if rows and rows[-1].end_s != start_s:
            raise ValueError("adjacent segments must be contiguous")
        rows.append(KeySegment(start_s=start_s, end_s=end_s, key=raw_key))
    return tuple(rows)


def load_segments_file(path: Path) -> tuple[KeySegment, ...]:
    """Read a local JSON annotation file. No network access is performed."""
    with path.open(encoding="utf-8") as annotation_file:
        payload = json.load(annotation_file)
    if not isinstance(payload, Mapping):
        raise ValueError("annotation JSON must be an object")  # noqa: TRY004 - input contract
    return load_segments(payload)


def load_verified_manifest_tracks(
    manifest_path: Path,
) -> dict[str, tuple[KeySegment, ...]]:
    """Load the verified timestamped fixtures from a local dataset manifest."""
    with manifest_path.open(encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    if not isinstance(manifest, Mapping):
        raise ValueError("manifest JSON must be an object")  # noqa: TRY004 - input contract
    fixtures = manifest.get("fixtures")
    if isinstance(fixtures, (str, bytes)) or not isinstance(fixtures, Sequence):
        raise ValueError("manifest fixtures must be an array")  # noqa: TRY004 - input contract

    tracks: dict[str, tuple[KeySegment, ...]] = {}
    for index, fixture in enumerate(fixtures):
        if not isinstance(fixture, Mapping):
            raise ValueError(  # noqa: TRY004 - input contract
                f"manifest fixture {index} must be an object"
            )
        track_id = fixture.get("id")
        if not isinstance(track_id, str) or not track_id:
            raise ValueError(f"manifest fixture {index} id must be a non-empty string")
        if track_id in tracks:
            raise ValueError(f"duplicate manifest fixture id: {track_id}")
        tracks[track_id] = load_segments(fixture)
    return tracks


__all__ = [
    "DATASET_DOI",
    "DATASET_LICENSE",
    "DATASET_NOTICE",
    "DATASET_SOURCE_URL",
    "KeySegment",
    "load_segments",
    "load_segments_file",
    "load_verified_manifest_tracks",
]
