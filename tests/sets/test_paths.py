"""Containment regressions for set-session filesystem paths."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.sets import paths as sets_paths


@pytest.mark.requirement("SET-03")
@pytest.mark.parametrize(
    "session_id",
    [
        "..",
        "../outside",
        r"..\outside",
        r"C:\outside",
        "/absolute/path",
        "nested/session",
        r"nested\session",
        ".",
        "",
        "bad\x00id",
    ],
)
def test_session_dir_rejects_uncontained_session_ids(
    sets_root: Path, session_id: str
) -> None:
    """An API-decoded Windows backslash must not escape the sets root."""
    with pytest.raises(sets_paths.SessionPathError):
        sets_paths.session_dir(session_id, root=sets_root)


@pytest.mark.requirement("SET-03")
def test_session_dir_rejects_existing_symlink_that_resolves_outside_root(
    sets_root: Path,
) -> None:
    outside = sets_root.parent / "outside"
    outside.mkdir()
    link = sets_root / "linked-session"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    with pytest.raises(sets_paths.SessionPathError):
        sets_paths.session_dir("linked-session", root=sets_root)


@pytest.mark.requirement("SET-03")
def test_session_dir_returns_resolved_direct_child(sets_root: Path) -> None:
    assert sets_paths.session_dir("session-1", root=sets_root) == (
        sets_root / "session-1"
    ).resolve()
