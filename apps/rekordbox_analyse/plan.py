"""Pure planner for the Rekordbox ANALYSE-ME flow (RBXAN-8322).

Given Open DJ's tracks and a read-only snapshot of Rekordbox's collection,
decide what one guarded write must do so Rekordbox can analyse every Open DJ
track it has not analysed yet. No I/O beyond the injected ``file_exists``.

Spec: ``(private path)``.

-Claude
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from apps.shared.rekordbox_db import is_streaming_path

# -----------------------------------------------------------------------------
# Config

ANALYSE_ME_PLAYLIST = "ANALYSE-ME"
VIVA_TARGET_PLAYLIST = "Viva 26 - master"
VIVA_SOURCE_PREFIX = "Viva"
MAX_ADD_PER_APPLY = 500
#: File types pyrekordbox ``add_content`` accepts (``pyrekordbox.db6.tables.FileType``).
SUPPORTED_SUFFIXES = frozenset({".mp3", ".m4a", ".flac", ".wav", ".aiff", ".aif"})

VivaStatus = Literal["ok", "missing", "is_folder", "none_needed"]
SkipReason = Literal["streaming", "no_file_path", "file_missing", "unsupported_type"]


# -----------------------------------------------------------------------------
# Inputs


@dataclass(frozen=True)
class OdjTrack:
    stable_id: str
    file_path: str | None
    playlists: tuple[str, ...] = ()


@dataclass(frozen=True)
class RbTrack:
    content_id: str
    folder_path: str
    analysed: bool


@dataclass(frozen=True)
class RbPlaylist:
    playlist_id: str
    name: str
    is_folder: bool
    member_ids: tuple[str, ...] = ()


# -----------------------------------------------------------------------------
# Output


@dataclass(frozen=True)
class AnalysePlan:
    master_db_sha256: str
    add_paths: tuple[str, ...]
    add_deferred: int
    playlist_add_ids: tuple[str, ...]
    playlist_remove_ids: tuple[str, ...]
    viva_add_paths: tuple[str, ...]
    viva_add_ids: tuple[str, ...]
    viva_status: VivaStatus
    analyse_me_exists: bool
    analysed_members: int
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def waiting(self) -> int:
        """Tracks that will sit in ANALYSE-ME, unanalysed, once this plan is applied."""
        return len(self.add_paths) + len(self.playlist_add_ids) + self._already_waiting

    _already_waiting: int = 0

    @property
    def is_noop(self) -> bool:
        return not (
            self.add_paths
            or self.playlist_add_ids
            or self.playlist_remove_ids
            or self.viva_add_paths
            or self.viva_add_ids
        )

    @property
    def plan_sha(self) -> str:
        """Digest of everything apply will do; apply refuses a plan whose digest differs."""
        payload = json.dumps(asdict(self), sort_keys=True, default=list)
        return hashlib.sha256(payload.encode()).hexdigest()

    def to_dict(self) -> dict[str, object]:
        body = asdict(self)
        body.pop("_already_waiting")
        body.update(waiting=self.waiting, is_noop=self.is_noop, plan_sha=self.plan_sha)
        return body


# -----------------------------------------------------------------------------
# Helpers


def normalise_path(path: str) -> str:
    """macOS hands back NFD names; Rekordbox stores what it was given. Compare in NFC."""
    return unicodedata.normalize("NFC", path)


def _skip_reason(track: OdjTrack, file_exists: Callable[[str], bool]) -> SkipReason | None:
    if not track.file_path:
        return "no_file_path"
    if is_streaming_path(track.file_path):
        return "streaming"
    if Path(track.file_path).suffix.lower() not in SUPPORTED_SUFFIXES:
        return "unsupported_type"
    if not file_exists(track.file_path):
        return "file_missing"
    return None


def _find_playlist(playlists: Iterable[RbPlaylist], name: str) -> RbPlaylist | None:
    matches = [pl for pl in playlists if pl.name == name]
    if len(matches) > 1:
        raise ValueError(f"Rekordbox has {len(matches)} playlists named {name!r}; rename all but one")
    return matches[0] if matches else None


def _wants_viva(track: OdjTrack) -> bool:
    return any(name.startswith(VIVA_SOURCE_PREFIX) for name in track.playlists)


def _viva_status(wanted: bool, viva: RbPlaylist | None) -> VivaStatus:
    if not wanted:
        return "none_needed"
    if viva is None:
        return "missing"
    if viva.is_folder:
        return "is_folder"
    return "ok"


# -----------------------------------------------------------------------------
# Planner


def build_plan(
    odj_tracks: Iterable[OdjTrack],
    rb_tracks: Iterable[RbTrack],
    rb_playlists: Iterable[RbPlaylist],
    master_db_sha256: str,
    *,
    file_exists: Callable[[str], bool] = lambda p: Path(p).is_file(),
    max_add: int = MAX_ADD_PER_APPLY,
) -> AnalysePlan:
    """Decide the one write that queues every unanalysed Open DJ track for Rekordbox."""
    playlists = list(rb_playlists)
    rb_by_path = {normalise_path(t.folder_path): t for t in rb_tracks}
    rb_by_id = {t.content_id: t for t in rb_by_path.values()}

    analyse_me = _find_playlist(playlists, ANALYSE_ME_PLAYLIST)
    if analyse_me is not None and analyse_me.is_folder:
        raise ValueError(f"{ANALYSE_ME_PLAYLIST!r} is a folder in Rekordbox; rename it")
    members = set(analyse_me.member_ids) if analyse_me else set()

    viva = _find_playlist(playlists, VIVA_TARGET_PLAYLIST)
    viva_members = set(viva.member_ids) if viva and not viva.is_folder else set()

    skipped: Counter[str] = Counter()
    add_paths: list[str] = []
    playlist_add: list[str] = []
    viva_paths: list[str] = []
    viva_ids: list[str] = []
    viva_wanted = False
    seen: set[str] = set()

    for track in odj_tracks:
        reason = _skip_reason(track, file_exists)
        if reason is not None:
            skipped[reason] += 1
            continue
        path = normalise_path(track.file_path or "")
        if path in seen:
            continue
        seen.add(path)
        wants_viva = _wants_viva(track)
        viva_wanted = viva_wanted or wants_viva
        existing = rb_by_path.get(path)
        if existing is None:
            add_paths.append(path)
            if wants_viva:
                viva_paths.append(path)
            continue
        if not existing.analysed and existing.content_id not in members:
            playlist_add.append(existing.content_id)
        if wants_viva and existing.content_id not in viva_members:
            viva_ids.append(existing.content_id)

    deferred = max(0, len(add_paths) - max_add)
    kept_paths = add_paths[:max_add]
    kept_set = set(kept_paths)
    viva_paths = [p for p in viva_paths if p in kept_set]

    viva_status = _viva_status(viva_wanted, viva)
    if viva_status != "ok":
        viva_paths, viva_ids = [], []

    remove = sorted(m for m in members if m in rb_by_id and rb_by_id[m].analysed)
    still_waiting = sum(1 for m in members if m in rb_by_id and not rb_by_id[m].analysed)

    return AnalysePlan(
        master_db_sha256=master_db_sha256,
        add_paths=tuple(kept_paths),
        add_deferred=deferred,
        playlist_add_ids=tuple(playlist_add),
        playlist_remove_ids=tuple(remove),
        viva_add_paths=tuple(viva_paths),
        viva_add_ids=tuple(viva_ids),
        viva_status=viva_status,
        analyse_me_exists=analyse_me is not None,
        analysed_members=len(remove),
        skipped=dict(sorted(skipped.items())),
        _already_waiting=still_waiting,
    )


__all__ = [
    "ANALYSE_ME_PLAYLIST",
    "MAX_ADD_PER_APPLY",
    "SUPPORTED_SUFFIXES",
    "VIVA_SOURCE_PREFIX",
    "VIVA_TARGET_PLAYLIST",
    "AnalysePlan",
    "OdjTrack",
    "RbPlaylist",
    "RbTrack",
    "build_plan",
    "normalise_path",
]
