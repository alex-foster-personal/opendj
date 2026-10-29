from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apps.shared import process_liveness as pl


def test_posix_kill_succeeds_reports_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] POSIX kill(pid, 0) succeeds [then] alive [else stop]."""
    monkeypatch.setattr(pl.os, "kill", lambda pid, sig: None)

    assert pl._posix_pid_is_alive(42) is True


def test_posix_process_lookup_error_reports_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] POSIX kill raises ProcessLookupError [then] dead [else stop]."""

    def _gone(pid: int, sig: int) -> None:
        raise ProcessLookupError(pid, sig)

    monkeypatch.setattr(pl.os, "kill", _gone)

    assert pl._posix_pid_is_alive(42) is False


def test_posix_permission_error_reports_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] POSIX kill raises PermissionError [then] alive [else stop]."""

    def _denied(pid: int, sig: int) -> None:
        raise PermissionError(pid, sig)

    monkeypatch.setattr(pl.os, "kill", _denied)

    assert pl._posix_pid_is_alive(42) is True


def test_win_open_process_handle_reports_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Windows OpenProcess returns a handle [then] alive after close [else stop]."""
    import ctypes

    kernel32 = MagicMock()
    kernel32.OpenProcess.return_value = 99
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: kernel32, raising=False)

    assert pl._win_pid_is_alive(42) is True
    kernel32.CloseHandle.assert_called_once_with(99)


def test_win_access_denied_reports_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Windows OpenProcess fails with access denied [then] alive [else stop]."""
    import ctypes

    kernel32 = MagicMock()
    kernel32.OpenProcess.return_value = 0
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: kernel32, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: pl._ERROR_ACCESS_DENIED, raising=False)

    assert pl._win_pid_is_alive(42) is True


def test_win_other_failure_reports_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Windows OpenProcess fails with another error [then] dead [else stop]."""
    import ctypes

    kernel32 = MagicMock()
    kernel32.OpenProcess.return_value = 0
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: kernel32, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 2, raising=False)

    assert pl._win_pid_is_alive(42) is False


def test_pid_is_alive_dispatches_to_posix_on_non_nt(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] os.name is not nt [then] POSIX arm runs [else stop]."""
    monkeypatch.setattr(pl.os, "name", "posix")
    monkeypatch.setattr(pl, "_posix_pid_is_alive", lambda pid: pid == 7)
    monkeypatch.setattr(pl, "_win_pid_is_alive", lambda pid: False)

    assert pl.pid_is_alive(7) is True
    assert pl.pid_is_alive(8) is False


def test_pid_is_alive_dispatches_to_windows_on_nt(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] os.name is nt [then] Windows arm runs [else stop]."""
    monkeypatch.setattr(pl.os, "name", "nt")
    monkeypatch.setattr(pl, "_posix_pid_is_alive", lambda pid: True)
    monkeypatch.setattr(pl, "_win_pid_is_alive", lambda pid: pid == 9)

    assert pl.pid_is_alive(9) is True
    assert pl.pid_is_alive(10) is False
