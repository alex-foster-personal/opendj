"""Playlist-writer protocol (Phase 3 stub).

Phase 3 (playlist-sync) ships in parallel. Until its writers land the
materialiser accepts any object implementing :class:`PlaylistWriter`.

TODO(phase-3): once ``apps.sync.playlists`` exports RBPlaylistWriter /
DjayPlaylistWriter, wire them into ``apps.smartlists.refresh`` via
``_build_writers()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class PlaylistWriter(Protocol):
    vendor: str

    def playlist_exists(self, name: str) -> bool: ...
    def create_playlist(self, name: str, track_ids: list[str]) -> None: ...
    def apply_diff(
        self, name: str, added: list[str], removed: list[str],
    ) -> None: ...


@dataclass
class FakeWriter:
    """In-memory PlaylistWriter for tests."""

    vendor: str = "fake"
    playlists: dict[str, list[str]] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)
    raise_on_apply: bool = False

    def playlist_exists(self, name: str) -> bool:
        self.calls.append(("exists", name))
        return name in self.playlists

    def create_playlist(self, name: str, track_ids: list[str]) -> None:
        self.calls.append(("create", name, list(track_ids)))
        if self.raise_on_apply:
            raise RuntimeError(f"{self.vendor} refused to create {name}")
        self.playlists[name] = list(track_ids)

    def apply_diff(
        self, name: str, added: list[str], removed: list[str],
    ) -> None:
        self.calls.append(("diff", name, list(added), list(removed)))
        if self.raise_on_apply:
            raise RuntimeError(f"{self.vendor} refused diff on {name}")
        current = self.playlists.setdefault(name, [])
        for rid in removed:
            if rid in current:
                current.remove(rid)
        for aid in added:
            if aid not in current:
                current.append(aid)


__all__ = ["PlaylistWriter", "FakeWriter"]
