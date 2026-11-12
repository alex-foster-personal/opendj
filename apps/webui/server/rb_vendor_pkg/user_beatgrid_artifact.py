"""Open DJ own PQTZ-shaped beatgrid artifact write/read (issue #11140).

Encodes the served own grid (including ``grid_edit`` apply) as a minimal PMAI
file with only PQTZ, using the quantize rules in ``apps.sync.analysis_writeback_pqtz``.
Read-back uses the same ``_beatgrid_payload`` / ``served_beats_from_pqtz_tag`` path
as rekordbox ANLZ. Never mutates a rekordbox ``.ANLZ`` file.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from apps.sync.analysis_writeback_pqtz import (
    build_minimal_dat_bytes,
    quantize_time_to_served_beat,
    refuse_dynamic_tempo_payload,
    served_beats_from_own,
    served_beats_from_pqtz_bytes,
)

from .own_beatgrid_overlay import _beatgrid_payload

USER_BEATGRID_SUBDIR = "user-beatgrid"
ARTIFACT_SUFFIX = ".DAT"


def artifact_path(data_dir: Path, stable_id: str) -> Path:
    """Path for this track's own PQTZ artifact under ``<data>/state/user-beatgrid/``."""
    return data_dir / "state" / USER_BEATGRID_SUBDIR / f"{stable_id}{ARTIFACT_SUFFIX}"


def write_served_grid_artifact(path: Path, payload: Mapping[str, object]) -> None:
    """Write ``payload``'s served beats as a fixed-tempo PQTZ artifact."""
    refuse_dynamic_tempo_payload(payload)
    beats = payload.get("beats") or []
    if not beats:
        raise ValueError("beatgrid has no beats to encode")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_minimal_dat_bytes(beats))


def read_served_beats_from_artifact(path: Path) -> list[dict[str, float | int]]:
    """Parse the artifact and return wire ``{n, bpm, t}`` beats."""
    if not path.is_file():
        raise FileNotFoundError(path)
    data = path.read_bytes()
    from pyrekordbox.anlz import AnlzFile

    pqtz = AnlzFile.parse(data).get_tag("PQTZ")
    if pqtz is None:
        raise ValueError(f"own beatgrid artifact {path} has no PQTZ tag")
    overlay_beats = _beatgrid_payload({"PQTZ": pqtz})[0]["beats"]
    tag_beats = served_beats_from_pqtz_bytes(data)
    if overlay_beats != tag_beats:
        raise ValueError("PQTZ read-back disagrees between overlay and tag decoder")
    return tag_beats


def quantize_against_artifact(path: Path, time_s: float) -> float:
    """Snap ``time_s`` to the nearest beat time from a read-back artifact."""
    return quantize_time_to_served_beat(read_served_beats_from_artifact(path), time_s)


def round_trip_served_beats(beats: Sequence[Mapping[str, object]]) -> list[dict[str, float | int]]:
    """Encode then decode ``beats``; the oracle is ``served_beats_from_own``."""
    return served_beats_from_pqtz_bytes(build_minimal_dat_bytes(beats))


__all__ = [
    "ARTIFACT_SUFFIX",
    "USER_BEATGRID_SUBDIR",
    "artifact_path",
    "quantize_against_artifact",
    "read_served_beats_from_artifact",
    "round_trip_served_beats",
    "write_served_grid_artifact",
]
