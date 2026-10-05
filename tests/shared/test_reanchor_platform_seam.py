"""Re-anchoring the share root on a platform with no anchored walk (LIBM-139).

Windows has no ``O_DIRECTORY`` / ``O_NOFOLLOW`` / ``dir_fd``, so the walk never
anchors a root there. Re-anchoring must say so, as its own refusal, rather
than reaching a POSIX-only flag and surfacing as an unclassified 500.

[if] the platform has no anchored walk [then] re-anchoring is refused as unavailable (501), [else stop].

-Claude
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from apps.shared import fd_anchored_walk, platform_paths
from apps.webui.server.routes import library as library_routes

pytestmark = pytest.mark.requirement("LIBM-139")


def test_reanchor_without_an_anchored_walk_is_refused_before_any_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the platform has no descriptor-anchored walk [then] re-anchoring raises RootReanchorUnavailable and opens nothing, [else stop].

    MUTATION TARGET: drop the support check and this reaches os.open with
    O_DIRECTORY, which Windows lacks (Codex, PR #4974).
    """
    monkeypatch.setattr(fd_anchored_walk, "FD_ANCHORED_WALK_SUPPORTED", False)

    def no_open(*_a: object, **_k: object) -> int:
        raise AssertionError("os.open reached on a platform without the anchored walk")

    monkeypatch.setattr(fd_anchored_walk.os, "open", no_open)
    with pytest.raises(fd_anchored_walk.RootReanchorUnavailable, match="does not have"):
        fd_anchored_walk.reanchor_root(tmp_path)


@pytest.mark.skipif(
    not fd_anchored_walk.FD_ANCHORED_WALK_SUPPORTED,
    reason="POSIX anchored walk (O_DIRECTORY, O_NOFOLLOW, dir_fd) is platform-specific",
)
def test_reanchor_with_the_anchored_walk_still_anchors(tmp_path: Path) -> None:
    """[if] the platform has the anchored walk [then] re-anchoring a real directory succeeds, [else stop].

    Overshoot control: the support check must not refuse everywhere.
    """
    root = tmp_path / "share"
    root.mkdir()
    assert fd_anchored_walk.reanchor_root(root) is True


def test_the_route_answers_501_where_the_root_is_never_anchored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] re-anchoring is unavailable on this platform [then] the route answers 501 naming why, not 409 or 500, [else stop]."""

    def unavailable() -> bool:
        raise fd_anchored_walk.RootReanchorUnavailable("this platform (win32) does not have it")

    monkeypatch.setattr(platform_paths, "reanchor_share_root", unavailable)
    with pytest.raises(HTTPException) as raised:
        library_routes.reanchor_share_root()
    assert raised.value.status_code == 501
    assert "win32" in str(raised.value.detail)
