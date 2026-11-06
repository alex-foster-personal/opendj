"""Track profiles from a one-time snapshot, so calibration builds need no running app (D10).

the maintainer closes the Open DJ desktop app when he is not using it (it is RAM hungry), and agents never launch it. A
profile is pure over two engine documents (``apps.autodj.profile.profile_from_payloads``): the track row
(``GET /api/v1/tracks/{id}``) and the analysis (``GET /api/v1/tracks/{id}/anlz?points=2048``). ``snapshot`` saves
both per track the next time an engine is up with his OK; ``CachedProfiles`` serves them afterwards. Audio paths
still resolve from the library's state.db, read only (``resolve_audio_path`` opens it ``mode=ro``).

Usage:
  python -m scripts.bench.tryer_calibration.cached_source snapshot --lock <engine lock> [--pairs <fixture>]
  (writes profiles/<stable_id>.json beside this file; an existing snapshot is refused, never overwritten)

Requirements:
  ✔︎ a cached profile is exactly what the engine documents build (same function, same points)
  ✔︎ a track with no snapshot is an explicit error naming the snapshot command, never a skip or a default
  ✔︎ a snapshot never overwrites one already taken
  ✔︎ a snapshot whose document stable_id does not match the requested track (or has no stable_id) is an explicit error, never served as that track
Acceptance:
  [if] no snapshot exists for a track [then ⛔️] CachedProfileMissing names it and the command
  [if] a snapshot holds no usable grid [then] PairNotTryable, as from the engine (the pair is skipped)
  [if] snapshot meets an existing file [then ⛔️] refuse
  [if] a snapshot's document stable_id does not match the requested id, or the field is missing [then ⛔️]
      CachedProfileMissing names the path, the expected id and the found id
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from apps.autodj.http import ProfileError, TrackProfile, build_service
from apps.shared.engine_origin import resolve_origin
from apps.transition_score.tryer import PROFILE_POINTS, EngineProfiles, PairNotTryable
from scripts.bench.tryer_calibration.pairs import FIXTURE_PATH, load_pairs_verified

PROFILES_DIR: Path = Path(__file__).parent / "profiles"
SNAPSHOT_SCHEMA = 1
_AUTODJ = build_service()


class CachedProfileMissing(RuntimeError):
    """No snapshot for a track: take one while an engine is up (with the maintainer's OK), never guess."""


def _snapshot_path(directory: Path, stable_id: str) -> Path:
    if not stable_id or "/" in stable_id or stable_id.startswith("."):
        raise ValueError(f"not a stable id: {stable_id!r}")
    return directory / f"{stable_id}.json"


@dataclass
class CachedProfiles:
    """``ProfileReader`` over snapshot files: profile_from_payloads on the saved track and anlz documents."""

    directory: Path = PROFILES_DIR
    _profiles: dict[str, TrackProfile] = field(default_factory=dict, repr=False)

    def profile(self, stable_id: str) -> TrackProfile:
        if stable_id in self._profiles:
            return self._profiles[stable_id]
        path = _snapshot_path(self.directory, stable_id)
        if not path.is_file():
            raise CachedProfileMissing(
                f"no profile snapshot for {stable_id} at {path}; take one while an engine is up: "
                "python -m scripts.bench.tryer_calibration.cached_source snapshot --lock <engine lock>"
            )
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema") != SNAPSHOT_SCHEMA:
            raise CachedProfileMissing(
                f"{path}: snapshot schema {document.get('schema')!r}, expected {SNAPSHOT_SCHEMA}"
            )
        found_id = document.get("stable_id")
        if found_id != stable_id:
            raise CachedProfileMissing(
                f"{path}: snapshot stable_id {found_id!r}, expected {stable_id!r}"
            )
        try:
            built = _AUTODJ.profile_from_payloads(document["anlz"], document["track"])
        except ProfileError as error:
            raise PairNotTryable(f"{stable_id}: no usable analysis: {error}") from error
        self._profiles[stable_id] = built
        return built


# -----------------------------------------------------------------------------
# snapshot (needs a running engine; agents never launch the app)
# -----------------------------------------------------------------------------
def snapshot(stable_ids: list[str], lock: Path, directory: Path = PROFILES_DIR) -> list[Path]:
    """Save each track's two engine documents; refuse before any write if one snapshot already exists."""
    targets = [_snapshot_path(directory, sid) for sid in stable_ids]
    taken = [str(t) for t in targets if t.exists()]
    if taken:
        raise FileExistsError(f"refusing to overwrite existing snapshots: {taken}")
    origin = resolve_origin(lock)
    engine = EngineProfiles(origin, _AUTODJ)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for stable_id, target in zip(stable_ids, targets, strict=True):
        quoted = quote(stable_id, safe="")
        document: dict[str, Any] = {
            "schema": SNAPSHOT_SCHEMA,
            "stable_id": stable_id,
            "captured_at": datetime.now(UTC).isoformat(),
            "engine": {"base_url": origin.base_url, "boot_id": origin.boot_id},
            "points": PROFILE_POINTS,
            "track": engine._get(f"/api/v1/tracks/{quoted}"),
            "anlz": engine._get(f"/api/v1/tracks/{quoted}/anlz?points={PROFILE_POINTS}"),
        }
        target.write_text(json.dumps(document) + "\n", encoding="utf-8")
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="save the fixture tracks' profile documents from a running engine")
    snap.add_argument("--lock", type=Path, required=True, help="the running engine's .engine.lock")
    snap.add_argument("--pairs", type=Path, default=FIXTURE_PATH)
    snap.add_argument("--out", type=Path, default=PROFILES_DIR)
    args = parser.parse_args(argv)
    pairs, _ = load_pairs_verified(args.pairs)
    ids = sorted({sid for p in pairs for sid in (p.out_id, p.in_id)})
    for path in snapshot(ids, args.lock, args.out):
        print(f"[OK] {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
