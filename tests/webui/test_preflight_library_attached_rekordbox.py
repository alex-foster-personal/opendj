"""P1-3 (issue #2722): rekordbox installed but library unreachable remediation.

When the Pioneer rekordbox app folder exists on disk but ``master.db`` is not
reachable (for example a removable drive is unplugged), ``library-attached``
must name that cause and offer a folder-import fallback -- not the generic
empty-library message.

[if] rekordbox is installed but master.db is unreachable [then] library-attached names that cause and offers import, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.webui.server.preflight_checks import (
    LIBRARY_ATTACHED_REKORDBOX_UNREACHABLE_USER_REMEDIATION,
    _rekordbox_library_unreachable,
    run_preflight,
)

pytestmark = pytest.mark.requirement("PREFLIGHT-03")


def _library_row(body: dict) -> dict:
    return next(check for check in body["checks"] if check["id"] == "library-attached")


def test_rekordbox_library_unreachable_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rb_dir = tmp_path / "rekordbox-app"
    rb_dir.mkdir()
    monkeypatch.setattr(
        "apps.shared.platform_paths.rekordbox_app_dir", lambda: rb_dir
    )
    assert _rekordbox_library_unreachable(data_dir) is True


def test_library_attached_names_unplugged_rekordbox_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "empty"
    data_dir.mkdir()
    state_path = data_dir / "state" / "state.db"

    rb_dir = tmp_path / "rekordbox-installed"
    rb_dir.mkdir()
    monkeypatch.setattr(
        "apps.shared.platform_paths.rekordbox_app_dir", lambda: rb_dir
    )

    body = run_preflight(state_path).model_dump()
    row = _library_row(body)

    assert row["status"] == "fail"
    assert "rekordbox" in row["user_detail"].lower()
    assert "not reachable" in row["user_detail"].lower()
    assert row["user_remediation"] == LIBRARY_ATTACHED_REKORDBOX_UNREACHABLE_USER_REMEDIATION
    assert "Plug in the drive" in row["user_remediation"]
    assert "import a folder" in row["user_remediation"].lower()


def test_generic_empty_library_when_rekordbox_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Pioneer folder -> the generic first-import message, not the USB copy."""
    data_dir = tmp_path / "empty"
    data_dir.mkdir()
    state_path = data_dir / "state" / "state.db"

    # Must not exist: an empty Pioneer folder still means "rekordbox installed
    # but library unreachable", which is the positive case above.
    missing = tmp_path / "no-rekordbox"
    monkeypatch.setattr(
        "apps.shared.platform_paths.rekordbox_app_dir", lambda: missing
    )

    body = run_preflight(state_path).model_dump()
    row = _library_row(body)

    assert row["status"] == "fail"
    assert row["user_detail"] == "No music imported yet."
    assert row["user_remediation"] is not None
    assert "Import your music" in row["user_remediation"]
    assert "Plug in the drive" not in row["user_remediation"]
