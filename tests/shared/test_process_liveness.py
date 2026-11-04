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


def test_posix_unexpected_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] POSIX kill raises other than lookup/permit [then] raise [else stop]."""

    def _boom(pid: int, sig: int) -> None:
        raise OSError("unexpected probe failure")

    monkeypatch.setattr(pl.os, "kill", _boom)

    with pytest.raises(OSError, match="unexpected probe failure"):
        pl._posix_pid_is_alive(42)


def _patch_win_kernel32(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import ctypes

    kernel32 = MagicMock()
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: kernel32, raising=False)
    return kernel32


def _assert_win32_signatures(kernel32: MagicMock) -> None:
    from ctypes import wintypes

    assert kernel32.OpenProcess.argtypes == [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]
    assert kernel32.CloseHandle.restype is wintypes.BOOL


def test_win_open_process_handle_reports_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Windows OpenProcess returns a handle [then] alive after close [else stop]."""
    kernel32 = _patch_win_kernel32(monkeypatch)
    kernel32.OpenProcess.return_value = 99
    kernel32.CloseHandle.return_value = True

    assert pl._win_pid_is_alive(42) is True
    kernel32.CloseHandle.assert_called_once_with(99)
    _assert_win32_signatures(kernel32)


def test_win_access_denied_reports_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Windows OpenProcess fails with access denied [then] alive [else stop]."""
    import ctypes

    kernel32 = _patch_win_kernel32(monkeypatch)
    kernel32.OpenProcess.return_value = 0
    monkeypatch.setattr(ctypes, "get_last_error", lambda: pl._ERROR_ACCESS_DENIED, raising=False)

    assert pl._win_pid_is_alive(42) is True
    _assert_win32_signatures(kernel32)


def test_win_invalid_parameter_reports_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Windows OpenProcess fails with error 87 [then] dead [else stop]."""
    import ctypes

    kernel32 = _patch_win_kernel32(monkeypatch)
    kernel32.OpenProcess.return_value = 0
    monkeypatch.setattr(
        ctypes, "get_last_error", lambda: pl._ERROR_INVALID_PARAMETER, raising=False
    )

    assert pl._win_pid_is_alive(42) is False
    _assert_win32_signatures(kernel32)


def test_win_unexpected_open_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Windows OpenProcess fails with unexpected error [then] raise [else stop]."""
    import ctypes

    win_error_codes: list[int] = []

    class FakeWinError(OSError):
        def __init__(self, code: int) -> None:
            self.winerror = code
            super().__init__(code, "fake WinError")

    def _win_error(code: int) -> FakeWinError:
        win_error_codes.append(code)
        return FakeWinError(code)

    kernel32 = _patch_win_kernel32(monkeypatch)
    kernel32.OpenProcess.return_value = 0
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 8, raising=False)
    monkeypatch.setattr(ctypes, "WinError", _win_error, raising=False)

    with pytest.raises(FakeWinError) as exc_info:
        pl._win_pid_is_alive(42)
    assert exc_info.value.winerror == 8
    assert win_error_codes == [8]


def test_win_close_handle_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Windows CloseHandle fails [then] raise WinError [else stop]."""
    import ctypes

    class FakeWinError(OSError):
        def __init__(self, code: int) -> None:
            self.winerror = code
            super().__init__(code, "fake WinError")

    kernel32 = _patch_win_kernel32(monkeypatch)
    kernel32.OpenProcess.return_value = 99
    kernel32.CloseHandle.return_value = False
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 6, raising=False)
    monkeypatch.setattr(ctypes, "WinError", FakeWinError, raising=False)

    with pytest.raises(FakeWinError) as exc_info:
        pl._win_pid_is_alive(42)
    assert exc_info.value.winerror == 6


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


def test_pid_is_alive_on_nt_never_calls_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] os.name is nt [then] pid_is_alive never calls os.kill [else stop]."""
    calls: list[tuple[int, int]] = []

    def _spy(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        raise AssertionError("os.kill must not run on nt")

    monkeypatch.setattr(pl.os, "name", "nt")
    monkeypatch.setattr(pl.os, "kill", _spy)
    kernel32 = _patch_win_kernel32(monkeypatch)
    kernel32.OpenProcess.return_value = 99
    kernel32.CloseHandle.return_value = True

    assert pl.pid_is_alive(42) is True
    assert calls == []


def test_pid_liveness_reexports_process_liveness() -> None:
    """[if] pid_liveness.pid_is_running is not process_liveness.pid_is_alive [then] split, [else stop]."""
    from apps.shared.pid_liveness import pid_is_running
    from apps.shared.process_liveness import pid_is_alive

    assert pid_is_running is pid_is_alive
