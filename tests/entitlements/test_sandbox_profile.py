"""The App Store build is a CONFIG of this build, and these pin what that means.

  [if] sandbox detection reads a build constant instead of the process
       container, a mis-packaged lane gets the wrong behaviour
       -> test_detection_uses_the_process_not_a_build_flag
  [if] /Volumes enumeration returns empty instead of refusing when sandboxed,
       a user is told every external drive is offline
       -> test_volume_scan_refuses_rather_than_returning_empty
  [if] the shipped store flag file names a flag nobody declared, the engine
       refuses to boot -> test_the_shipped_store_profile_only_sets_real_flags
  [if] usb.export stops defaulting on, every non-store build loses USB
       -> test_usb_export_is_on_by_default
"""

from __future__ import annotations

import json

import pytest

from apps.feature_flags import profiles
from apps.feature_flags.store import FLAGS
from apps.shared import sandbox

# Resolved through the profile registry, not by path: the file moved once
# already (out of the Tauri dir into apps/feature_flags/profiles/) and a
# hardcoded path made three tests fail for a reason unrelated to what they
# test.
STORE_PROFILE = profiles.profile_path("appstore")


def test_detection_uses_the_process_not_a_build_flag() -> None:
    """Platform is passed explicitly so this runs identically on a Linux CI box.

    Without it these assertions pass on a developer's Mac and silently assert
    nothing on the Linux pytest lane, where every call short-circuits to False.
    """
    mac = {"platform": "darwin"}
    assert sandbox.is_sandboxed({"HOME": "/Users/dj"}, **mac) is False
    assert sandbox.is_sandboxed({"APP_SANDBOX_CONTAINER_ID": "com.opendj.desktop"}, **mac)
    assert sandbox.is_sandboxed(
        {"HOME": "/Users/dj/Library/Containers/com.opendj.desktop/Data"}, **mac
    )


def test_the_sandbox_is_macos_only() -> None:
    """A Linux or Windows host is never sandboxed, whatever the environment says."""
    container = {"APP_SANDBOX_CONTAINER_ID": "com.opendj.desktop"}
    assert sandbox.is_sandboxed(container, platform="linux") is False
    assert sandbox.is_sandboxed(container, platform="win32") is False


def test_volume_scan_refuses_rather_than_returning_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of SAND-02: silence is the bug, not the blocked read."""
    from apps.reconcile import match

    monkeypatch.setattr(sandbox, "is_sandboxed", lambda *a, **k: True)
    monkeypatch.setattr(match, "refuse_if_sandboxed", sandbox.refuse_if_sandboxed)
    with pytest.raises(sandbox.SandboxRefusal, match="App Store build"):
        match.mounted_volume_names()


def test_the_shipped_store_profile_only_sets_real_flags() -> None:
    """An undeclared key in the file makes the engine refuse to boot."""
    declared = {flag.flag_id for flag in FLAGS}
    profile = json.loads(STORE_PROFILE.read_text())
    unknown = sorted(set(profile) - declared)
    assert not unknown, (
        f"the appstore profile sets undeclared flag(s): {unknown}. The "
        "flag store raises FlagFileError on an undeclared key, so this would "
        "stop the store build from starting at all."
    )
    assert all(isinstance(v, bool) for v in profile.values())


def test_the_store_profile_turns_usb_export_off() -> None:
    assert json.loads(STORE_PROFILE.read_text())["usb.export"] is False


def test_usb_export_is_on_by_default() -> None:
    """Off in the store build only. Every other build keeps USB."""
    usb = next(f for f in FLAGS if f.flag_id == "usb.export")
    assert usb.default is True
