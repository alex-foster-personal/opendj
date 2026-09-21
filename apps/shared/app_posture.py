"""Gig vs Prep app-level resource posture (PERFMODE-03).

Agent-native parity: ``python -m apps.engine_core.app_posture``.
Operator write-up: ``app_docs/0fe48ec6_gig-prep-posture.md``.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

PREP_LIBRARY_POLL_MS: int = 60_000
GIG_LIBRARY_POLL_MS: int = 300_000
GIG_PREFETCH_TRACKS: int = 2
GIG_PREFETCH_BYTES: int = 24 * 1024 * 1024
GIG_PREVIEW_PCM_BYTES: int = 64 * 1024 * 1024


class AppPosture(StrEnum):
    PREP = "prep"
    GIG = "gig"


_POSTURE_LABELS: dict[AppPosture, str] = {
    AppPosture.PREP: "Prep",
    AppPosture.GIG: "Gig",
}


class InvalidAppPosture(ValueError):
    """User posture string is not prep|gig."""


def parse_posture(raw: str | None) -> AppPosture:
    if raw is None:
        return AppPosture.PREP
    value = raw.strip().lower()
    if value == "prep":
        return AppPosture.PREP
    if value == "gig":
        return AppPosture.GIG
    raise InvalidAppPosture(f"app_posture must be prep|gig, got {raw!r}")


def read_posture_from_prefs(data_dir: Path) -> AppPosture:
    path = data_dir / "state" / "ui-prefs.json"
    if not path.is_file():
        return AppPosture.PREP
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppPosture.PREP
    if not isinstance(raw, dict):
        return AppPosture.PREP
    value = raw.get("app_posture")
    if value is None:
        return AppPosture.PREP
    if not isinstance(value, str):
        return AppPosture.PREP
    try:
        return parse_posture(value)
    except InvalidAppPosture:
        return AppPosture.PREP


def _resolve_posture(posture: AppPosture | None, data_dir: Path | None) -> AppPosture:
    if posture is not None:
        return posture
    from apps.shared.paths import DATA_DIR

    return read_posture_from_prefs(data_dir if data_dir is not None else DATA_DIR)


def library_poll_ms(posture: AppPosture | None = None, *, data_dir: Path | None = None) -> int:
    resolved = _resolve_posture(posture, data_dir)
    if resolved == AppPosture.GIG:
        return GIG_LIBRARY_POLL_MS
    return PREP_LIBRARY_POLL_MS


def apply_posture_to_workers(
    count: int,
    posture: AppPosture | None = None,
    *,
    data_dir: Path | None = None,
) -> int:
    resolved = _resolve_posture(posture, data_dir)
    if resolved == AppPosture.GIG:
        return max(1, count // 2)
    return count


def apply_posture_to_prefetch(
    tracks: int,
    nbytes: int,
    posture: AppPosture | None = None,
    *,
    data_dir: Path | None = None,
) -> tuple[int, int]:
    resolved = _resolve_posture(posture, data_dir)
    if resolved == AppPosture.GIG:
        return min(tracks, GIG_PREFETCH_TRACKS), min(nbytes, GIG_PREFETCH_BYTES)
    return tracks, nbytes


def posture_wire(data_dir: Path | None = None) -> dict[str, object]:
    from apps.shared.paths import DATA_DIR

    data = data_dir if data_dir is not None else DATA_DIR
    posture = read_posture_from_prefs(data)
    worker_divisor: int = 2 if posture == AppPosture.GIG else 1
    prefetch_tracks_floor: int | None = GIG_PREFETCH_TRACKS if posture == AppPosture.GIG else None
    prefetch_bytes_floor: int | None = GIG_PREFETCH_BYTES if posture == AppPosture.GIG else None
    return {
        "posture": posture.value,
        "label": _POSTURE_LABELS[posture],
        "scalers": {
            "library_poll_ms": library_poll_ms(posture),
            "worker_divisor": worker_divisor,
            "prefetch_tracks_floor": prefetch_tracks_floor,
            "prefetch_bytes_floor": prefetch_bytes_floor,
        },
    }


def main(argv: list[str] | None = None) -> int:
    _ = argv
    print(json.dumps(posture_wire(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
