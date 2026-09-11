"""Load LYR-01 measured scorer fixtures with checksum and provenance checks."""

from __future__ import annotations

import json
from pathlib import Path

from apps.shared.hashing import sha256_file

MEASURED_FIXTURE_SET = "lyrics-alignment-jamendo-round3a-measured-v1"

_TRACK_KEYS = frozenset({"track_id", "measured", "reference_onsets_s", "predicted_onsets_s"})


def load_measured_fixture(path: Path, expected_sha256: str) -> dict:
    """Load a LYR-01 measured fixture after checksum + provenance checks.

    ``expected_sha256`` is required (no default). It must be a separately
    pinned ``sha256:<64-hex>`` string, not a digest stored inside the JSON.
    """
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ValueError(
            f"fixture checksum for {path} does not match: expected {expected_sha256}, got {actual}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate_measured_payload(payload)
    return payload


def _validate_measured_payload(payload: dict) -> None:
    if payload.get("fixture_set") != MEASURED_FIXTURE_SET:
        raise ValueError(
            f"fixture_set must be {MEASURED_FIXTURE_SET!r}, got {payload.get('fixture_set')!r}"
        )
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("missing provenance block")
    if not provenance.get("measured_utc"):
        raise ValueError("missing provenance.measured_utc")
    if not provenance.get("scorer_version"):
        raise ValueError("missing provenance.scorer_version")
    if "corpus_measured" not in payload:
        raise ValueError("missing corpus_measured")
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        raise ValueError("missing tracks list")
    for track in tracks:
        if not isinstance(track, dict):
            raise ValueError("track entry must be an object")
        if not str(track.get("track_id", "")).startswith("jl-"):
            raise ValueError(f"track_id must start with jl-: {track.get('track_id')!r}")
        extra_keys = set(track) - _TRACK_KEYS
        if extra_keys:
            raise ValueError(
                f"track {track.get('track_id')!r} carries disallowed keys: {sorted(extra_keys)}"
            )
