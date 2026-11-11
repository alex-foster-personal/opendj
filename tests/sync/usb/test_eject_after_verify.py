"""Eject-after-verify: a volume is ejected only after its copy verified clean.

[if] verification is absent, malformed or failed [then] eject never runs, [else stop].

``apps.sync.usb.verify.eject_after_verify`` takes the verify receipt, the
volume root and identity, and an eject operation. Every refusal raises
``EjectRefused`` before the eject operation is called. Receipts here are real
``verify_drive`` reports over real copied bytes, never hand-made "OK" objects,
except where a test needs a malformed one on purpose.

On macOS a disposable disk image is created and attached with ``hdiutil``
under the real ``/Volumes`` and ejected with ``hdiutil detach``. Off macOS
those tests skip as UNAVAILABLE and the same function is driven against a
``tmp_path`` volume with a recording eject operation.

Regression one-liners:
  - if a mounted disk image with a clean receipt is left mounted then broken
  - if verification with one failure reaches the eject operation then broken
  - if an absent or malformed receipt is treated as verified then broken
  - if a successful eject's receipt omits the volume identity then broken
"""

from __future__ import annotations

import copy
import plistlib
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.sync.usb.diff import compute_plan
from apps.sync.usb.profile import Profile
from apps.sync.usb.state import CanonicalTrack
from apps.sync.usb.verify import (
    EjectReceipt,
    EjectRefused,
    eject_after_verify,
    report_to_jsonable,
    verify_drive,
)
from apps.webui.server.routes import usb_eject

pytestmark = pytest.mark.requirement("USBEXP-12673")

VOLUME_ID = "vol:11111111-2222-3333-4444-555555555555"
VOLUMES = Path("/Volumes")
HDIUTIL = Path("/usr/bin/hdiutil")
DISKUTIL = Path("/usr/sbin/diskutil")

on_macos = pytest.mark.skipif(
    sys.platform != "darwin",
    reason=f"UNAVAILABLE: needs macOS ({HDIUTIL}, {DISKUTIL}, /Volumes); this host is {sys.platform}",
)


class RecordingEject:
    """An eject operation that records every call instead of unmounting."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __call__(self, root: Path) -> None:
        self.calls.append(root)


def _copy_to(profile: Profile, canonical: list[CanonicalTrack], root: Path) -> None:
    plan = compute_plan(profile=profile, canonical=canonical, drive_root=root)
    for op in plan.ops:
        assert op.src is not None
        op.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(op.src, op.dst)


@pytest.fixture
def copied(fixture_profile: Profile, fixture_canonical: list[CanonicalTrack], drive_root: Path) -> Path:
    _copy_to(fixture_profile, fixture_canonical, drive_root)
    return drive_root


def _verify(profile: Profile, canonical: list[CanonicalTrack], root: Path):
    return verify_drive(profile=profile, canonical=canonical, drive_root=root)


def _refused(receipt: object, root: Path, *, identity: str = VOLUME_ID) -> tuple[EjectRefused, RecordingEject]:
    eject = RecordingEject()
    with pytest.raises(EjectRefused) as caught:
        eject_after_verify(receipt, volume_root=root, volume_identity=identity, eject=eject)
    assert eject.calls == [], "a refused verification must never reach the eject operation"
    return caught.value, eject


# ---------------------------------------------------------------- clean copy


def test_clean_receipt_ejects_once_and_names_the_volume(fixture_profile, fixture_canonical, copied) -> None:
    report = _verify(fixture_profile, fixture_canonical, copied)
    eject = RecordingEject()
    receipt = eject_after_verify(report, volume_root=copied, volume_identity=VOLUME_ID, eject=eject)
    assert eject.calls == [copied]
    assert receipt == EjectReceipt(volume_root=str(copied), volume_identity=VOLUME_ID, verified_files=3, ejected=True)


def test_clean_json_receipt_ejects(fixture_profile, fixture_canonical, copied) -> None:
    report = report_to_jsonable(_verify(fixture_profile, fixture_canonical, copied))
    eject = RecordingEject()
    receipt = eject_after_verify(report, volume_root=copied, volume_identity=VOLUME_ID, eject=eject)
    assert eject.calls == [copied]
    assert receipt.volume_identity == VOLUME_ID
    assert receipt.volume_root == str(copied)


# ------------------------------------------------------- one failure refuses


def _break_missing(root: Path) -> None:
    (root / "Alice" / "AA" / "One.mp3").unlink()


def _break_corrupt(root: Path) -> None:
    (root / "Alice" / "AA" / "One.mp3").write_bytes(b"corrupt")


def _break_extra(root: Path) -> None:
    extra = root / "Stray" / "Thing.mp3"
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b"not in the plan")


def _break_playlist(root: Path) -> None:
    (root / "Playlists").mkdir()
    (root / "Playlists" / "Warmup.m3u8").write_text("#EXTM3U\n../Does/Not/Exist.mp3\n", encoding="utf-8")


@pytest.mark.parametrize("breaker", [_break_missing, _break_corrupt, _break_extra, _break_playlist])
def test_one_verification_failure_never_ejects(fixture_profile, fixture_canonical, copied, breaker) -> None:
    breaker(copied)
    report = _verify(fixture_profile, fixture_canonical, copied)
    refused, _ = _refused(report, copied)
    assert refused.code == "verification_failed"
    refused, _ = _refused(report_to_jsonable(report), copied)
    assert refused.code == "verification_failed"


# ------------------------------------------------ absent or malformed refuses


def test_absent_receipt_is_not_verified(copied) -> None:
    refused, _ = _refused(None, copied)
    assert refused.code == "verification_absent"


def test_receipt_that_verified_nothing_is_not_verified(fixture_profile, copied) -> None:
    refused, _ = _refused(_verify(fixture_profile, [], copied), copied)
    assert refused.code in {"verification_absent", "verification_failed"}


def _set(path: tuple[object, ...], value: object):
    def mutate(receipt: dict) -> None:
        target = receipt
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _drop(key: str):
    def mutate(receipt: dict) -> None:
        del receipt[key]

    return mutate


MALFORMED = {
    "counts_missing": _drop("counts"),
    "files_not_list": _set(("files",), "all good"),
    "ok_count_is_text": _set(("counts", "ok"), "3"),
    "count_is_bool": _set(("counts", "missing"), False),
    "negative_count": _set(("counts", "missing"), -1),
    "count_disagrees": _set(("counts", "ok"), 99),
    "drift_hidden_in_files": _set(("files", 0, "status"), "missing"),
    "unknown_status": _set(("files", 0, "status"), "fine"),
    "unhashable_list_status": _set(("files", 0, "status"), ["ok"]),
    "unhashable_dict_status": _set(("files", 0, "status"), {"ok": 1}),
    "file_not_mapping": _set(("files", 0), "ok"),
    "broken_not_list": _set(("playlists_broken",), None),
    "drive_root_missing": _drop("drive_root"),
    "drive_uuid_mistyped": _set(("drive_uuid",), 7),
}


@pytest.mark.parametrize("mutate", MALFORMED.values(), ids=MALFORMED.keys())
def test_malformed_receipt_is_not_verified(fixture_profile, fixture_canonical, copied, mutate) -> None:
    receipt = copy.deepcopy(report_to_jsonable(_verify(fixture_profile, fixture_canonical, copied)))
    mutate(receipt)
    refused, _ = _refused(receipt, copied)
    assert refused.code in {"verification_malformed", "verification_wrong_volume"}


@pytest.mark.parametrize("receipt", ["ok", b"ok", 0, True, [], object()], ids=repr)
def test_non_report_receipt_is_malformed(copied, receipt) -> None:
    refused, _ = _refused(receipt, copied)
    assert refused.code == "verification_malformed"


def test_receipt_for_another_volume_is_refused(fixture_profile, fixture_canonical, copied, tmp_path) -> None:
    other = tmp_path / "OTHER"
    other.mkdir()
    _copy_to(fixture_profile, fixture_canonical, other)
    refused, _ = _refused(_verify(fixture_profile, fixture_canonical, other), copied)
    assert refused.code == "verification_wrong_volume"


def test_receipt_for_another_volume_uuid_is_refused(fixture_profile, fixture_canonical, copied) -> None:
    receipt = report_to_jsonable(_verify(fixture_profile, fixture_canonical, copied))
    receipt["drive_uuid"] = "99999999-0000-0000-0000-000000000000"
    refused, _ = _refused(receipt, copied)
    assert refused.code == "verification_wrong_volume"


# ---------------------------------------------------------- volume identity


@pytest.mark.parametrize("identity", ["", "   ", None], ids=repr)
def test_missing_identity_refuses_before_eject(fixture_profile, fixture_canonical, copied, identity) -> None:
    report = _verify(fixture_profile, fixture_canonical, copied)
    refused, _ = _refused(report, copied, identity=identity)  # type: ignore[arg-type]
    assert refused.code == "volume_identity_missing"


def test_an_eject_receipt_cannot_omit_the_volume_identity() -> None:
    with pytest.raises(ValueError):
        EjectReceipt(volume_root="/Volumes/GIG", volume_identity="", verified_files=1, ejected=True)


# ------------------------------------------------------- route-side wiring


def test_route_helper_refuses_before_probe_or_lock(fixture_profile, fixture_canonical, copied, monkeypatch) -> None:
    probed: list[object] = []
    monkeypatch.setattr(usb_eject, "_eject_device", lambda *a: probed.append(a))
    monkeypatch.setattr(usb_eject, "eject_operation", lambda *a: pytest.fail("lock taken for a refused eject"))
    _break_corrupt(copied)
    report = _verify(fixture_profile, fixture_canonical, copied)
    receipts: tuple[object, ...] = (report, None, {"counts": {}})
    for receipt in receipts:
        with pytest.raises(EjectRefused):
            usb_eject.eject_after_verified_copy("/usr/sbin/diskutil", copied, VOLUME_ID, receipt)
    assert probed == []


def test_route_helper_ejects_the_validated_device_after_a_clean_receipt(
    fixture_profile, fixture_canonical, copied, monkeypatch
) -> None:
    probed: list[tuple[str, Path, str]] = []

    def fake_eject_device(command: str, root: Path, volume_id: str) -> str:
        probed.append((command, root, volume_id))
        return "ejected"

    monkeypatch.setattr(usb_eject, "_eject_device", fake_eject_device)
    report = _verify(fixture_profile, fixture_canonical, copied)
    receipt = usb_eject.eject_after_verified_copy("/usr/sbin/diskutil", copied, VOLUME_ID, report)
    assert probed == [("/usr/sbin/diskutil", copied, VOLUME_ID)]
    assert receipt.volume_identity == VOLUME_ID
    assert receipt.ejected is True


# ------------------------------------------------- macOS disposable disk image


def _is_mounted(path: Path) -> bool:
    proc = subprocess.run([str(DISKUTIL), "info", "-plist", str(path)], capture_output=True, check=False, timeout=30)
    return proc.returncode == 0 and plistlib.loads(proc.stdout).get("MountPoint") == str(path)


def _volume_uuid(path: Path) -> str:
    proc = subprocess.run([str(DISKUTIL), "info", "-plist", str(path)], capture_output=True, check=True, timeout=30)
    return f"vol:{plistlib.loads(proc.stdout).get('VolumeUUID', '')}"


@pytest.fixture
def disk_image(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """A real disk image attached under /Volumes: ``(mount, whole-disk device)``; detached in teardown."""
    name = f"ODJ-EAV-TEST-{uuid.uuid4().hex[:8]}"
    image = tmp_path / f"{name}.dmg"
    subprocess.run(
        [str(HDIUTIL), "create", "-size", "8m", "-fs", "HFS+", "-volname", name, str(image)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    attached = subprocess.run(
        [str(HDIUTIL), "attach", "-nobrowse", "-plist", str(image)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    entities = plistlib.loads(attached.stdout)["system-entities"]
    mounts = [Path(e["mount-point"]) for e in entities if e.get("mount-point")]
    whole = min((e["dev-entry"] for e in entities if e.get("dev-entry")), key=len)
    assert len(mounts) == 1, f"hdiutil attached {mounts}, expected one mount"
    try:
        assert mounts[0].parent == VOLUMES, f"hdiutil mounted {mounts[0]}, not under {VOLUMES}"
        assert _is_mounted(mounts[0]), "control: the image is mounted before the test"
        yield mounts[0], whole
    finally:
        if mounts[0].exists():
            detached = subprocess.run([str(HDIUTIL), "detach", "-force", whole], capture_output=True, check=False)
            assert detached.returncode == 0, f"hdiutil could not detach {whole}"


def _hdiutil_detach(whole: str):
    def _eject(_root: Path) -> None:
        subprocess.run([str(HDIUTIL), "detach", whole], capture_output=True, check=True, timeout=60)

    return _eject


@on_macos
def test_disk_image_with_clean_receipt_is_ejected(fixture_profile, fixture_canonical, disk_image) -> None:
    mount, whole = disk_image
    _copy_to(fixture_profile, fixture_canonical, mount)
    identity = _volume_uuid(mount)
    report = _verify(fixture_profile, fixture_canonical, mount)
    receipt = eject_after_verify(report, volume_root=mount, volume_identity=identity, eject=_hdiutil_detach(whole))
    assert not _is_mounted(mount), "a verified disk image must not stay mounted"
    assert receipt.volume_identity == identity
    assert receipt.volume_root == str(mount)


@on_macos
@pytest.mark.parametrize("breaker", [_break_missing, _break_corrupt])
def test_disk_image_with_failed_receipt_stays_mounted(fixture_profile, fixture_canonical, disk_image, breaker) -> None:
    mount, whole = disk_image
    _copy_to(fixture_profile, fixture_canonical, mount)
    breaker(mount)
    report = _verify(fixture_profile, fixture_canonical, mount)
    with pytest.raises(EjectRefused):
        eject_after_verify(report, volume_root=mount, volume_identity=_volume_uuid(mount), eject=_hdiutil_detach(whole))
    assert _is_mounted(mount), "a failed verification must leave the image mounted"


@on_macos
def test_disk_image_with_absent_receipt_stays_mounted(disk_image) -> None:
    mount, whole = disk_image
    with pytest.raises(EjectRefused):
        eject_after_verify(None, volume_root=mount, volume_identity=_volume_uuid(mount), eject=_hdiutil_detach(whole))
    assert _is_mounted(mount), "an absent verification must leave the image mounted"
