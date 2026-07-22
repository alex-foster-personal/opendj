"""Path-safety regression tests for Rekordbox session-history exports."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.audit import session_history


@pytest.mark.parametrize(
    "history_name",
    [
        "../../escaped",
        "/tmp/escaped",
        r"C:\\Users\\dj\\escaped",
        r"HISTORY\\..\\escaped",
        "////",
    ],
)
def test_save_keeps_rekordbox_history_name_within_sessions_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history_name: str,
) -> None:
    """A database-controlled History name must never select an output path."""
    monkeypatch.setattr(session_history.paths, "PROJECT_ROOT", tmp_path)

    csv_path = session_history._save(history_name, [])
    sessions_dir = (tmp_path / "maintainer" / "sessions").resolve()

    assert csv_path.resolve().is_relative_to(sessions_dir)
    assert csv_path.parent == sessions_dir
    assert csv_path.suffix == ".csv"


def test_save_uses_a_deterministic_safe_slug_for_equivalent_separators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unix and Windows separators produce the same basename-safe filename."""
    monkeypatch.setattr(session_history.paths, "PROJECT_ROOT", tmp_path)

    unix_path = session_history._save("HISTORY/escaped", [])
    windows_path = session_history._save(r"HISTORY\\escaped", [])

    assert unix_path == windows_path
    assert unix_path.name == "escaped.csv"
