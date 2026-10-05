"""USB volume discovery finds diskutil by absolute path, never through PATH.

[if] USB discovery looks diskutil up through PATH [then] fail, [else stop].

Found on the launchd-run preview engine (Thu 1 Oct 2026): the agent's PATH
was ``~/.local/bin:/opt/homebrew/...:/usr/local/bin:/usr/bin:/bin``, which
has no ``/usr/sbin``, so ``shutil.which("diskutil")`` answered None and every
``GET /api/v1/usb/volumes`` was a 503 ``diskutil_unavailable`` on a Mac whose
diskutil was sitting at ``/usr/sbin/diskutil`` the whole time.

Regression one-liners:
  - if discovery consults PATH to find diskutil then broken
  - if a missing diskutil resolves to a path instead of None then broken
  - if a non-executable diskutil resolves to a path then broken
  - if a darwin host with /usr/sbin/diskutil reports diskutil_unavailable under a launchd PATH then broken
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.shared import macos_diskutil
from apps.webui.server.routes import usb_volumes as usb_mod
from apps.webui.server.routes.usb_gate import UsbExportGate

pytestmark = [pytest.mark.requirement("USBPLAY-11")]

_ENABLED_GATE = UsbExportGate(flag_enabled=True, refusal=None)
#: The PATH launchd handed the preview engine: no /usr/sbin.
_LAUNCHD_PATH = "/usr/local/bin:/usr/bin:/bin"


def _path_lookup_is_a_failure(name: str, *_args: object, **_kwargs: object) -> str:
    raise AssertionError(f"diskutil must not be looked up on PATH (which({name!r}))")


def test_the_pinned_path_is_absolute_and_in_usr_sbin() -> None:
    assert Path("/usr/sbin/diskutil") == macos_diskutil.DISKUTIL_PATH
    assert macos_diskutil.DISKUTIL_PATH.is_absolute()


def test_an_executable_file_resolves_to_its_own_absolute_path(tmp_path: Path) -> None:
    tool = tmp_path / "diskutil"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    assert macos_diskutil.resolve_diskutil(tool) == str(tool)


def test_a_missing_tool_resolves_to_none(tmp_path: Path) -> None:
    assert macos_diskutil.resolve_diskutil(tmp_path / "diskutil") is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX permission bits: Windows os.access ignores X_OK, so chmod 0o644 cannot make a file non-executable",
)
def test_a_non_executable_file_resolves_to_none(tmp_path: Path) -> None:
    tool = tmp_path / "diskutil"
    tool.write_text("not a program")
    tool.chmod(0o644)
    assert macos_diskutil.resolve_diskutil(tool) is None


def test_system_discovery_never_consults_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The class fix, on any platform: whatever PATH says, discovery uses the
    pinned file. PATH lookup is made a hard failure, not merely an empty one,
    so a `which` that happens to succeed on the test host cannot pass this."""
    tool = tmp_path / "diskutil"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    volumes = tmp_path / "Volumes"
    volumes.mkdir()
    monkeypatch.setattr(shutil, "which", _path_lookup_is_a_failure)
    monkeypatch.setenv("PATH", _LAUNCHD_PATH)
    monkeypatch.setattr(macos_diskutil, "DISKUTIL_PATH", tool)
    monkeypatch.setattr(usb_mod, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(usb_mod, "_VOLUMES_ROOT", volumes)
    monkeypatch.setattr(usb_mod, "is_sandboxed", lambda: False)

    discovery = usb_mod._system_discovery(usb_export_gate=_ENABLED_GATE)

    assert discovery.diskutil_command == str(tool)


def test_system_discovery_still_fails_loudly_when_the_pinned_file_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overshoot control: pinning the path must not turn a Mac that
    really has no diskutil into one that pretends to."""
    volumes = tmp_path / "Volumes"
    volumes.mkdir()
    monkeypatch.setattr(macos_diskutil, "DISKUTIL_PATH", tmp_path / "absent")
    monkeypatch.setattr(usb_mod, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(usb_mod, "_VOLUMES_ROOT", volumes)
    monkeypatch.setattr(usb_mod, "is_sandboxed", lambda: False)

    with pytest.raises(usb_mod.UsbDiscoveryUnavailable) as excinfo:
        usb_mod._system_discovery(usb_export_gate=_ENABLED_GATE)

    assert excinfo.value.reason == "diskutil_unavailable"


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/sbin/diskutil").is_file(),
    reason="needs a real macOS diskutil",
)
def test_a_real_mac_resolves_diskutil_under_the_launchd_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", _LAUNCHD_PATH)
    monkeypatch.setattr(usb_mod, "is_sandboxed", lambda: False)

    discovery = usb_mod._system_discovery(usb_export_gate=_ENABLED_GATE)

    assert discovery.diskutil_command == "/usr/sbin/diskutil"
