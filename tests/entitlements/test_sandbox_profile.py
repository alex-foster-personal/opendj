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
from pathlib import Path

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


# ----- SAND-01: the fourth refusal state ----------------------------------
#
#   [if] the App Store refusal is any of the other three sentences, a user is
#        told the wrong fact about why a control is dead
#        -> test_the_fourth_refusal_is_none_of_the_other_three
#   [if] a refusal sends the user to a download outside the store, the build
#        is a guideline 3.2.2(vi) rejection risk
#        -> test_no_sandbox_refusal_advertises_a_way_out_of_the_store
#   [if] /flags does not say which profile is active, the UI has to guess
#        which build it is running in -> test_flags_names_the_build_and_the_container
#   [if] a flag the store profile turned off carries no refusal, the panel has
#        nothing to render -> test_a_flag_the_store_profile_turned_off_is_refused
#   [if] the same flag off in a FULL build carries one anyway, the fourth
#        state has started lying -> test_a_full_build_refuses_nothing
#   [if] a sandboxed USB scan reports volumes_root_unavailable, the user is
#        told their Mac has no /Volumes rather than that this build may not
#        look -> test_sandboxed_usb_discovery_blames_the_sandbox_not_the_host


def test_the_fourth_refusal_is_none_of_the_other_three() -> None:
    """Four facts, four sentences. The python half; the four-way pin with the
    frontend's own constants is
    apps/webui/frontend/tests/unit/store-build-refusal.test.mjs."""
    from apps.entitlements import UI_REFUSAL_TITLE

    fourth = sandbox.STORE_BUILD_REFUSAL_TITLE
    assert fourth != UI_REFUSAL_TITLE
    assert fourth != "not implemented - see PARITY-TODO"
    lowered = fourth.lower()
    assert "plan" not in lowered, "the store refusal must not blame the plan"
    assert "not implemented" not in lowered, "the feature IS implemented"
    assert "app store" in lowered, "it must name the build it is about"


def _sandbox_refusal_literals() -> list[str]:
    """Every string literal handed to ``refuse_if_sandboxed`` under apps/.

    Parsed rather than grepped so a call spanning four lines is read as one
    call. This is a CLASS check, not a check of the one site that exists
    today: the next capability to refuse gets its own call, and the cheapest
    place for a 3.2.2(vi) rejection to ship is a helpful sentence in it.
    """
    import ast

    root = Path(__file__).resolve().parents[2] / "apps"
    found: list[str] = []
    for source_file in root.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(
                node.func, "attr", None
            )
            if name not in {
                "refuse_if_sandboxed",
                "store_build_refusal_message",
            }:
                continue
            for value in [*node.args, *(kw.value for kw in node.keywords)]:
                if isinstance(value, ast.Constant) and isinstance(
                    value.value, str
                ):
                    found.append(value.value)
                elif isinstance(value, ast.JoinedStr):
                    found.extend(
                        part.value
                        for part in value.values
                        if isinstance(part, ast.Constant)
                        and isinstance(part.value, str)
                    )
    return found


def test_no_sandbox_refusal_advertises_a_way_out_of_the_store() -> None:
    """Guideline 3.2.2(vi), enforced on the strings that would break it.

    Apple bars an app from requiring a user to download something else to
    access functionality, and reads "get our other build to unlock this" as
    circumventing the store. Shipping a store build WITHOUT the feature is
    fine; advertising the way around it from inside that build is not. See
    specs/appstore-sandbox-remediation.md section 3, option B.
    """
    literals = _sandbox_refusal_literals()
    # The control: a probe that finds no call sites would pass this vacuously.
    assert literals, (
        "no refuse_if_sandboxed / store_build_refusal_message call sites were "
        "parsed out of apps/, so this test measured nothing"
    )
    banned = ("download", "dmg", "non-store", "open-dj.com", "outside the app store")
    for literal in literals:
        lowered = literal.lower()
        for phrase in banned:
            assert phrase not in lowered, (
                f"a sandbox refusal string names {phrase!r}:\n  {literal}\n"
                "That is the guideline 3.2.2(vi) pattern. State the absence; "
                "do not route the user out of the store."
            )


def test_sandboxed_usb_discovery_blames_the_sandbox_not_the_host(
    tmp_path: Path,
) -> None:
    """The order of the checks in _resolve_discovery is the whole test.

    Sandboxed, /Volumes does not read as a directory, so a is_dir() probe
    placed first answers ``volumes_root_unavailable`` -- "this Mac has no
    /Volumes", a fault of the machine. The truth is a build that may not look,
    and the two are different sentences to a user and different next steps to
    an agent.
    """
    from apps.webui.server.routes import usb_volumes

    missing_root = tmp_path / "no-such-volumes"
    with pytest.raises(usb_volumes.UsbDiscoveryUnavailable) as blocked:
        usb_volumes._resolve_discovery(
            platform_name="darwin",
            volumes_root=missing_root,
            diskutil_command="/usr/sbin/diskutil",
            sandboxed=True,
        )
    assert blocked.value.reason == "app_sandbox_forbids_volume_listing"
    assert blocked.value.ui_title == sandbox.STORE_BUILD_REFUSAL_TITLE
    assert blocked.value.to_detail()["ui_title"] == sandbox.STORE_BUILD_REFUSAL_TITLE

    # The control, on the SAME unreachable root: unsandboxed, the identical
    # call must still report the host fault, and must carry no ui_title --
    # a broken machine is an error, not one of the four refusals.
    with pytest.raises(usb_volumes.UsbDiscoveryUnavailable) as host_fault:
        usb_volumes._resolve_discovery(
            platform_name="darwin",
            volumes_root=missing_root,
            diskutil_command="/usr/sbin/diskutil",
            sandboxed=False,
        )
    assert host_fault.value.reason == "volumes_root_unavailable"
    assert host_fault.value.ui_title is None
    assert "ui_title" not in host_fault.value.to_detail()
