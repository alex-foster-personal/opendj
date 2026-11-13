"""Verify receipts are bound to the volume UUID they verified.

[if] a different stick is swapped in at the same mount path after verification [then] eject never runs, [else stop].

Acceptance (issue #12742): a receipt from ``verify_drive`` against a mounted
volume carries that volume's UUID; a different stick swapped in at the same
mount path makes ``eject_after_verify`` refuse without ejecting; the same
stick still mounted still ejects with a successful receipt.

The UUID lookup is injectable (``verify_drive(volume_uuid_lookup=...)``), so
these run on Linux with a fake lookup. The real ``diskutil`` cases attach
disposable ``hdiutil`` images under ``/Volumes`` and skip off macOS.

Regression one-liners:
  - if a receipt from stick A ejects stick B mounted at the same path then broken
  - if a receipt with no volume UUID is treated as verified then broken
  - if a stick swapped mid-verify yields a bound receipt then broken
  - if a verified disk image's receipt lacks its diskutil VolumeUUID then broken
"""

from __future__ import annotations

import plistlib
import subprocess
import uuid
from pathlib import Path

import pytest

from apps.shared import macos_diskutil
from apps.sync.usb.marker import probe_volume_uuid
from apps.sync.usb.verify import (
    EjectRefused,
    eject_after_verify,
    report_to_jsonable,
    verify_drive,
)
from apps.webui.server.routes import usb_eject
from tests.sync.usb.test_eject_after_verify import (
    DISKUTIL,
    HDIUTIL,
    VOLUMES,
    RecordingEject,
    _copy_to,
    _hdiutil_detach,
    _is_mounted,
    _volume_uuid,
    copied,
    disk_image,
    on_macos,
)

__all__ = ["copied", "disk_image"]  # fixtures re-used from the eject tests

pytestmark = pytest.mark.requirement("USBEXP-12761")

STICK_A = "AAAAAAAA-1111-2222-3333-444444444444"
STICK_B = "BBBBBBBB-5555-6666-7777-888888888888"


def _lookup(*answers: str | None):
    """A fake UUID lookup that answers in order and repeats its last answer."""
    calls: list[Path] = []

    def lookup(root: Path) -> str | None:
        calls.append(root)
        return answers[min(len(calls), len(answers)) - 1]

    lookup.calls = calls  # type: ignore[attr-defined]
    return lookup


def _verify(profile, canonical, root: Path, lookup):
    return verify_drive(profile=profile, canonical=canonical, drive_root=root, volume_uuid_lookup=lookup)


def _refused(receipt: object, root: Path, identity: str) -> EjectRefused:
    eject = RecordingEject()
    with pytest.raises(EjectRefused) as caught:
        eject_after_verify(receipt, volume_root=root, volume_identity=identity, eject=eject)
    assert eject.calls == [], "a refused receipt must never reach the eject operation"
    return caught.value


# ------------------------------------------------------------ capture


def test_receipt_carries_the_verified_volume_uuid(fixture_profile, fixture_canonical, copied) -> None:
    lookup = _lookup(STICK_A)
    report = _verify(fixture_profile, fixture_canonical, copied, lookup)
    assert report.drive_uuid == STICK_A
    assert report_to_jsonable(report)["drive_uuid"] == STICK_A
    assert set(lookup.calls) == {copied}  # type: ignore[attr-defined]


def test_lookup_answer_is_normalised(fixture_profile, fixture_canonical, copied) -> None:
    report = _verify(fixture_profile, fixture_canonical, copied, _lookup(f" vol:{STICK_A.lower()} "))
    assert report.drive_uuid == STICK_A


@pytest.mark.parametrize("answer", [None, "", "   "], ids=repr)
def test_unreadable_uuid_leaves_the_receipt_unbound(fixture_profile, fixture_canonical, copied, answer) -> None:
    report = _verify(fixture_profile, fixture_canonical, copied, _lookup(answer))
    assert report.drive_uuid is None


def test_failing_lookup_leaves_the_receipt_unbound(fixture_profile, fixture_canonical, copied) -> None:
    def lookup(_root: Path) -> str | None:
        raise OSError("diskutil vanished")

    report = _verify(fixture_profile, fixture_canonical, copied, lookup)
    assert report.drive_uuid is None
    assert _refused(report, copied, f"vol:{STICK_A}").code == "verification_unbound"


def test_stick_swapped_during_verification_is_not_bound(fixture_profile, fixture_canonical, copied) -> None:
    report = _verify(fixture_profile, fixture_canonical, copied, _lookup(STICK_A, STICK_B))
    assert report.drive_uuid is None
    for identity in (f"vol:{STICK_A}", f"vol:{STICK_B}"):
        assert _refused(report, copied, identity).code == "verification_unbound"


def test_default_lookup_without_diskutil_is_unbound(fixture_profile, fixture_canonical, copied, monkeypatch) -> None:
    monkeypatch.setattr(macos_diskutil, "DISKUTIL_PATH", copied / "no-such-diskutil")
    assert probe_volume_uuid(copied) is None
    report = verify_drive(profile=fixture_profile, canonical=fixture_canonical, drive_root=copied)
    assert report.drive_uuid is None
    assert _refused(report, copied, f"vol:{STICK_A}").code == "verification_unbound"


# ------------------------------------------------------- swap refuses


def test_different_stick_at_the_same_path_is_refused(fixture_profile, fixture_canonical, copied) -> None:
    report = _verify(fixture_profile, fixture_canonical, copied, _lookup(STICK_A))
    assert _refused(report, copied, f"vol:{STICK_B}").code == "verification_wrong_volume"
    assert _refused(report_to_jsonable(report), copied, f"vol:{STICK_B}").code == "verification_wrong_volume"


def test_volume_without_uuid_is_refused(fixture_profile, fixture_canonical, copied) -> None:
    report = _verify(fixture_profile, fixture_canonical, copied, _lookup(STICK_A))
    assert _refused(report, copied, "path:FIXTURE-A").code == "verification_wrong_volume"


@pytest.mark.parametrize("value", [None, "", "  "], ids=repr)
def test_json_receipt_without_uuid_is_not_verified(fixture_profile, fixture_canonical, copied, value) -> None:
    receipt = report_to_jsonable(_verify(fixture_profile, fixture_canonical, copied, _lookup(STICK_A)))
    receipt["drive_uuid"] = value
    assert _refused(receipt, copied, f"vol:{STICK_A}").code == "verification_unbound"


def test_json_receipt_with_uuid_key_dropped_is_not_verified(fixture_profile, fixture_canonical, copied) -> None:
    receipt = report_to_jsonable(_verify(fixture_profile, fixture_canonical, copied, _lookup(STICK_A)))
    del receipt["drive_uuid"]
    assert _refused(receipt, copied, f"vol:{STICK_A}").code == "verification_unbound"


def test_route_helper_refuses_a_swapped_stick_before_probe_or_lock(
    fixture_profile, fixture_canonical, copied, monkeypatch
) -> None:
    monkeypatch.setattr(usb_eject, "_eject_device", lambda *a: pytest.fail("probed a swapped stick"))
    monkeypatch.setattr(usb_eject, "eject_operation", lambda *a: pytest.fail("lock taken for a swapped stick"))
    report = _verify(fixture_profile, fixture_canonical, copied, _lookup(STICK_A))
    with pytest.raises(EjectRefused) as caught:
        usb_eject.eject_after_verified_copy(str(DISKUTIL), copied, f"vol:{STICK_B}", report)
    assert caught.value.code == "verification_wrong_volume"


# --------------------------------------------------- same stick ejects


@pytest.mark.parametrize("identity", [f"vol:{STICK_A}", STICK_A, f"vol:{STICK_A.lower()}"])
def test_same_stick_still_ejects(fixture_profile, fixture_canonical, copied, identity) -> None:
    report = _verify(fixture_profile, fixture_canonical, copied, _lookup(STICK_A))
    for receipt in (report, report_to_jsonable(report)):
        eject = RecordingEject()
        done = eject_after_verify(receipt, volume_root=copied, volume_identity=identity, eject=eject)
        assert eject.calls == [copied]
        assert done.ejected is True
        assert done.volume_identity == identity


# ---------------------------------------------------- real diskutil (macOS)


def _attach_image(tmp_path: Path, name: str) -> tuple[Path, str]:
    image = tmp_path / f"{name}-{uuid.uuid4().hex[:6]}.dmg"
    subprocess.run(
        [str(HDIUTIL), "create", "-size", "8m", "-fs", "HFS+", "-volname", name, str(image)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    attached = subprocess.run(
        [str(HDIUTIL), "attach", "-nobrowse", "-plist", str(image)], capture_output=True, check=True, timeout=60
    )
    entities = plistlib.loads(attached.stdout)["system-entities"]
    mounts = [Path(e["mount-point"]) for e in entities if e.get("mount-point")]
    whole = min((e["dev-entry"] for e in entities if e.get("dev-entry")), key=len)
    assert len(mounts) == 1 and mounts[0].parent == VOLUMES, f"hdiutil attached {mounts}"
    return mounts[0], whole


@on_macos
@pytest.mark.host_mutation
def test_real_diskutil_binds_the_disk_image_uuid(fixture_profile, fixture_canonical, disk_image) -> None:
    mount, whole = disk_image
    _copy_to(fixture_profile, fixture_canonical, mount)
    identity = _volume_uuid(mount)
    report = verify_drive(profile=fixture_profile, canonical=fixture_canonical, drive_root=mount)
    assert f"vol:{report.drive_uuid}" == identity, "the receipt must carry diskutil's VolumeUUID"
    done = eject_after_verify(report, volume_root=mount, volume_identity=identity, eject=_hdiutil_detach(whole))
    assert done.ejected and not _is_mounted(mount)


@on_macos
def test_real_swap_at_the_same_mount_path_stays_mounted(fixture_profile, fixture_canonical, tmp_path) -> None:
    name = f"ODJ-UUID-TEST-{uuid.uuid4().hex[:8]}"
    first, first_whole = _attach_image(tmp_path, name)
    _copy_to(fixture_profile, fixture_canonical, first)
    report = verify_drive(profile=fixture_profile, canonical=fixture_canonical, drive_root=first)
    first_identity = _volume_uuid(first)
    subprocess.run([str(HDIUTIL), "detach", first_whole], capture_output=True, check=True, timeout=60)
    second, second_whole = _attach_image(tmp_path, name)
    try:
        assert second == first, "control: the second stick mounts at the same path"
        second_identity = _volume_uuid(second)
        assert second_identity != first_identity, "control: the second stick is a different volume"
        with pytest.raises(EjectRefused) as caught:
            eject_after_verify(
                report, volume_root=second, volume_identity=second_identity, eject=_hdiutil_detach(second_whole)
            )
        assert caught.value.code == "verification_wrong_volume"
        assert _is_mounted(second), "a swapped stick must stay mounted"
    finally:
        if second.exists():
            subprocess.run([str(HDIUTIL), "detach", "-force", second_whole], capture_output=True, check=False)
