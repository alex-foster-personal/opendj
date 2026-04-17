"""Regression tests for codex CONFIRMED-FOLLOWUP findings, group A.

Covers:
* P02-F2 -- :class:`apps.sync.fingerprint.FingerprintCache` invalidates
  stale entries when the backing audio file mutates on disk.
* P03-04 -- :func:`apps.sync.playlist_plan.build_plan` raises on
  canonical-name collisions instead of silently collapsing two djay
  playlists into one op.
* P04-03 -- :func:`apps.sync.safety.require_cautious_before_bulk` blocks
  bulk live writes unless a cautious stage has recorded success.

P03-03 is a control-flow reorder (reversal script written before first
destructive write) with no new data invariant to assert beyond "the CLI
main still works"; the existing ``test_playlist_apply_integration``
suite covers that path.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from apps.sync import fingerprint as fp_mod
from apps.sync.fingerprint import FingerprintCache
from apps.sync.playlist_plan import (
    DjayPlaylistRead,
    MatchSet,
    PlaylistPlanError,
    build_plan,
)
from apps.sync.safety import (
    SafetyAbort,
    mark_cautious_success,
    require_cautious_before_bulk,
)


pytestmark = [
    pytest.mark.requirement("SYNC-02"),
    pytest.mark.requirement("SYNC-03"),
    pytest.mark.requirement("SYNC-04"),
]


class _FakeAcoustid(types.ModuleType):
    NoBackendError = type("NoBackendError", (Exception,), {})

    def __init__(self) -> None:
        super().__init__("acoustid")
        self.calls: list[str] = []

    def fingerprint_file(self, path: str):  # noqa: D401
        self.calls.append(path)
        return (42.0, f"FP:{Path(path).read_bytes().decode(errors='replace')}")

    def compare_fingerprints(self, a, b):  # pragma: no cover - unused here
        return 0.0


@pytest.fixture
def fake_acoustid(monkeypatch: pytest.MonkeyPatch) -> _FakeAcoustid:
    mod = _FakeAcoustid()
    monkeypatch.setitem(sys.modules, "acoustid", mod)
    fp_mod._BACKEND_WARNED = False
    return mod


def test_p02_f2_cache_invalidates_on_mtime_change(
    tmp_path: Path, fake_acoustid: _FakeAcoustid
) -> None:
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"v1")
    cache = FingerprintCache(tmp_path / "fp.sqlite")
    try:
        first = cache.get_or_compute(audio)
        # Modify the file: new bytes + bump mtime explicitly in case of FS
        # mtime resolution coarseness on some platforms.
        audio.write_bytes(b"v2-longer")
        os.utime(audio, (audio.stat().st_atime, audio.stat().st_mtime + 10))
        second = cache.get_or_compute(audio)
    finally:
        cache.close()

    assert first is not None and second is not None
    assert first[1] != second[1], (
        "cache must recompute when the audio file content changes on disk"
    )
    assert len(fake_acoustid.calls) == 2


def test_p02_f2_cache_hits_when_file_unchanged(
    tmp_path: Path, fake_acoustid: _FakeAcoustid
) -> None:
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"stable")
    cache = FingerprintCache(tmp_path / "fp.sqlite")
    try:
        cache.get_or_compute(audio)
        cache.get_or_compute(audio)
    finally:
        cache.close()
    assert len(fake_acoustid.calls) == 1


def _rb_playlist_stub(name: str, rb_id: str, track_ids: list[str]):
    """Build the minimal ``FlatPlaylist`` input that ``build_plan`` needs."""
    from apps.sync.playlist_plan import FlatPlaylist

    return FlatPlaylist(
        rb_id=rb_id,
        flat_name=name,
        parent_path=(),
        track_ids_ordered=tuple(track_ids),
    )


def test_p03_04_canonical_collision_raises() -> None:
    rb = [_rb_playlist_stub("Set 1", "rb-1", [])]
    djay = [
        DjayPlaylistRead(uuid="u-a", name="Set 1", member_uuids_ordered=()),
        # NFC vs NFD form for the same visual string would collide in
        # practice; we simulate the collapse with a pure duplicate by
        # canonical name (different uuid, same visible name).
        DjayPlaylistRead(uuid="u-b", name="Set 1", member_uuids_ordered=()),
    ]
    matches = MatchSet(rb_to_djay={}, djay_to_rb={})

    with pytest.raises(PlaylistPlanError, match="P03-04"):
        build_plan(rb, djay, matches)


def test_p04_03_bulk_blocked_without_cautious(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SafetyAbort, match="P04-03"):
        require_cautious_before_bulk("apply_ratings")


def test_p04_03_bulk_allowed_after_cautious_stamp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    stamp = mark_cautious_success("apply_ratings")
    assert stamp.exists()
    # Should not raise.
    require_cautious_before_bulk("apply_ratings")


def test_p04_03_override_bypasses_check(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # Should not raise even without a stamp when override=True.
    require_cautious_before_bulk("apply_ratings", override=True)
