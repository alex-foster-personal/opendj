"""Safely eject one USB stick (USBEXP-02).

POST /api/v1/usb/eject   {"mount_path": "/Volumes/<label>", "volume_id": "vol:<VolumeUUID>"}

Eject does not write to the stick; it asks macOS to flush and unmount it so
the user can pull it out. It runs only where volume discovery runs (the same
gate, platform, sandbox and pinned-diskutil checks as ``GET /usb/volumes``),
only on one direct child of the volumes root, and only when ``diskutil info``
says the volume is external, removable and on the USB bus: an internal disk,
a disk image or a non-USB drive is refused, never ejected.

Success is proven, not assumed: the route answers 200 only after
``diskutil eject`` exits 0 AND the mount point is gone. A clean exit with the
mount still present is a 409 ``eject_unverified``, never a success.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apps.sync.usb.verify import EjectReceipt, eject_after_verify
from apps.webui.server.routes import usb_volumes
from apps.webui.server.routes.usb_classify import classify_role, root_has_dj_export
from apps.webui.server.routes.usb_gate import usb_export_gate
from apps.webui.server.routes.usb_root_access import (
    await_root_listing,
    root_listing_deadline,
    start_root_listing,
)
from apps.webui.server.routes.usb_volume_lock import VolumeBusy, eject_operation, hold_unsettled_eject

router = APIRouter(prefix="/usb", tags=["usb"])
MOUNT_UNDER_API_PREFIX = True

_DISKUTIL_TIMEOUT_S = 30.0
#: How long a timed-out eject keeps every volume held after it returns.
_EJECT_SETTLE_S = 2 * _DISKUTIL_TIMEOUT_S


class UsbEjectIn(BaseModel):
    mount_path: str
    # The listing id the caller picked (`vol:<VolumeUUID>` from GET /usb/volumes).
    # The probe must find that same volume at mount_path, or nothing is ejected.
    volume_id: str


class UsbEjectOut(BaseModel):
    mount_path: str
    name: str
    ejected: bool
    message: str


class UsbEjectErrorDetail(BaseModel):
    code: str
    message: str
    ui_title: str | None = None


class UsbEjectErrorOut(BaseModel):
    detail: UsbEjectErrorDetail


class UsbEjectValidationErrorOut(BaseModel):
    """FastAPI's own 422 for a malformed request body (a missing ``volume_id``,
    a mistyped field): ``detail`` is its list of field errors, not ``{code,
    message}`` (Sol P1 4286542573)."""

    detail: list[dict[str, Any]]


class _EjectRefused(RuntimeError):
    def __init__(self, status: int, code: str, message: str, ui_title: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.ui_title = ui_title

    def response(self) -> JSONResponse:
        detail: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.ui_title is not None:
            detail["ui_title"] = self.ui_title
        return JSONResponse(status_code=self.status, content={"detail": detail})


def _run_diskutil(command: str, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run the pinned diskutil with a bounded wait."""
    return subprocess.run(
        [command, *args],
        capture_output=True,
        check=False,
        timeout=_DISKUTIL_TIMEOUT_S,
    )


def _target_root(mount_path: str, volumes_root: Path) -> Path:
    requested = Path(mount_path)
    if requested.is_symlink():
        raise _EjectRefused(422, "eject_target_invalid", "the mount path must not be a symlink")
    try:
        root = requested.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise _EjectRefused(
            404, "eject_target_missing", f"nothing is mounted at {mount_path}"
        ) from exc
    if not root.is_dir() or root.parent != volumes_root.resolve():
        raise _EjectRefused(
            422,
            "eject_target_invalid",
            f"only a volume directly under {volumes_root} can be ejected, got {root}",
        )
    return root


def _diskutil_info(stdout: bytes) -> dict[str, Any]:
    """Decode ``diskutil info -plist`` output, or refuse as ``eject_probe_failed``.

    Truncated XML raises expat's error (not a ``ValueError``), and a well-formed
    plist whose root is not a dict has no fields to read: both are a probe
    that failed, never an unstructured 500.
    """
    try:
        info = plistlib.loads(stdout)
    except (plistlib.InvalidFileException, ValueError, ExpatError) as exc:
        raise _EjectRefused(409, "eject_probe_failed", f"diskutil info gave no plist: {exc}") from exc
    if not isinstance(info, dict):
        raise _EjectRefused(
            409,
            "eject_probe_failed",
            f"diskutil info gave a {type(info).__name__} plist, not a dictionary of disk fields",
        )
    return info


_DEVICE_ID = re.compile(r"disk[0-9]+(s[0-9]+)*")


def _device_identifier(info: dict[str, Any]) -> str:
    """The BSD device the probe validated (``disk4s1``), or refuse.

    Eject is sent to this identity, never the path: a different volume can
    mount at the same ``/Volumes/<label>`` between the probe and the eject,
    and a path-based eject would unmount that unvalidated device.
    """
    device = info.get("DeviceIdentifier")
    if not isinstance(device, str) or _DEVICE_ID.fullmatch(device) is None:
        raise _EjectRefused(
            409,
            "eject_probe_failed",
            f"diskutil info gave no usable device identifier ({device!r}), so Open DJ cannot tell what it would eject",
        )
    return device


def _require_identity(info: dict[str, Any], volume_id: str) -> None:
    """Refuse unless the probed volume is the one the caller picked.

    A cached listing can still name a stick that was pulled out while another
    mounted at the same path; only the VolumeUUID tells them apart.
    """
    expected = volume_id.removeprefix("vol:") if volume_id.startswith("vol:") else ""
    if expected == "":
        raise _EjectRefused(
            409,
            "eject_identity_unverifiable",
            f"the listing has no volume UUID for this stick ({volume_id!r}), so Open DJ cannot prove "
            "the volume at this path is the one picked; eject it from the Finder",
        )
    probed = info.get("VolumeUUID")
    if probed != expected:
        raise _EjectRefused(
            409,
            "eject_target_changed",
            f"the volume at this path is not the stick that was picked (expected {expected}, found {probed!r}); "
            "rescan and pick it again",
        )


def _safety_fields(info: dict[str, Any]) -> tuple[bool, bool, str]:
    """``(Internal, ejectable, BusProtocol)`` from the probe, or refuse.

    A missing or mistyped field is a failed probe, never a "no": reading it as
    False or as an empty bus would classify an answer Open DJ did not
    understand (Sol P1 4286542596). Ejectable is ``Ejectable`` or
    ``RemovableMedia``; at least one must be present, and each present one a
    boolean.
    """
    internal = info.get("Internal")
    bus = info.get("BusProtocol")
    removable_flags = [info[k] for k in ("Ejectable", "RemovableMedia") if k in info]
    if (
        not isinstance(internal, bool)
        or not isinstance(bus, str)
        or not removable_flags
        or not all(isinstance(flag, bool) for flag in removable_flags)
    ):
        raise _EjectRefused(
            409,
            "eject_probe_failed",
            "diskutil info gave incomplete or malformed safety fields (Internal, Ejectable or "
            f"RemovableMedia, BusProtocol): Internal={internal!r}, BusProtocol={bus!r}, "
            f"Ejectable/RemovableMedia={removable_flags!r}",
        )
    return internal, any(removable_flags), bus


def _require_removable(command: str, root: Path, volume_id: str) -> str:
    """Validate ``root`` as the picked external removable USB volume; return its device id."""
    try:
        proc = _run_diskutil(command, ["info", "-plist", str(root)])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _EjectRefused(409, "eject_probe_failed", f"diskutil info failed: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise _EjectRefused(409, "eject_probe_failed", f"diskutil info rejected {root}: {stderr}")
    info = _diskutil_info(proc.stdout)
    if info.get("MountPoint") != str(root):
        raise _EjectRefused(
            409, "eject_probe_failed", "diskutil reports a different mount point for this path"
        )
    _require_identity(info, volume_id)
    internal, ejectable, bus = _safety_fields(info)
    external = internal is False
    removable = ejectable
    # The same bus rule the export target probe uses: a disk image, SD reader
    # or Thunderbolt drive also reports external + ejectable, and is refused.
    on_usb = "usb" in bus.lower()
    if not (external and removable and on_usb):
        raise _EjectRefused(
            409,
            "eject_target_not_removable",
            f"{root.name} is not an external removable USB volume, so Open DJ will not eject it",
        )
    _require_usb_stick(root, info, internal=internal, bus=bus)
    return _device_identifier(info)


def _require_usb_stick(root: Path, info: dict[str, Any], *, internal: bool, bus: str) -> None:
    """Refuse anything the volume listing would not show as a ``usb_stick``.

    ``Ejectable`` alone also passes a fixed USB backup disk (Ejectable true,
    RemovableMedia false). The listing tells those apart with the same
    classifier, from RemovableMedia and a DJ export at the root, so a direct
    caller holding a backup disk's UUID is refused here exactly as the screen
    never offers it (Sol P1 4292553903).
    """
    removable_media = info.get("RemovableMedia")
    if not isinstance(removable_media, bool):
        # Without RemovableMedia the classifier calls any readable USB volume a
        # stick, a fixed backup disk included, so the probe is incomplete for
        # eject even though Ejectable alone satisfied the field check (Codex P1
        # 4301939256).
        raise _EjectRefused(
            409,
            "eject_probe_failed",
            f"diskutil info gave no RemovableMedia answer ({removable_media!r}), so Open DJ cannot "
            "tell this stick from a fixed USB disk and will not eject it",
        )
    listing = await_root_listing(start_root_listing(root), deadline_mono=root_listing_deadline())
    root_listed = listing.access == "ok"
    if removable_media is not True and not root_listed:
        # The listing keeps an unreadable USB volume as a stick so the user can
        # still find it, but for a fixed disk only the DJ export at its root
        # tells a stick from a backup disk, so eject fails closed (Codex P1
        # 4300723940).
        raise _EjectRefused(
            409,
            "eject_target_not_usb_stick",
            f"{root.name} could not be read, so Open DJ cannot tell it from a fixed USB disk "
            "and will not eject it",
        )
    role = classify_role(
        protocol=bus,
        removable=removable_media,
        has_dj_export=root_listed and root_has_dj_export(listing.names),
        internal=internal,
        root_listed=root_listed,
    )
    if role != "usb_stick":
        raise _EjectRefused(
            409,
            "eject_target_not_usb_stick",
            f"{root.name} is a {role.replace('_', ' ')}, not a DJ USB stick, so Open DJ will not eject it",
        )


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": UsbEjectErrorOut, "description": text}
    for status, text in (
        (404, "Nothing is mounted at that path."),
        (409, "diskutil refused, failed, or the mount is still present after eject."),
        (503, "USB discovery is unavailable on this host or build, so eject is too."),
    )
}
# Two shapes share 422: this route's own path refusal ({code, message}) and
# FastAPI's request validation (a list of field errors), so both are declared.
_ERROR_RESPONSES[422] = {
    "model": UsbEjectErrorOut | UsbEjectValidationErrorOut,
    "description": "The path is not a direct child of the volumes root ({code, message}), "
    "or the request body is malformed, such as a missing volume_id (a list of field errors).",
}


def _eject_device(command: str, root: Path, volume_id: str) -> str:
    """Probe ``root``, eject the device it validated, prove the mount is gone; return diskutil's words."""
    device = _require_removable(command, root, volume_id)
    try:
        proc = _run_diskutil(command, ["eject", device])
    except subprocess.TimeoutExpired as exc:
        # The unmount may still finish after the wait; never call it failed,
        # and keep writes off every volume until it has had time to land
        # (Codex P1 4352472811).
        hold_unsettled_eject(root, _EJECT_SETTLE_S)
        state = "is still mounted" if root.exists() else "is no longer mounted"
        raise _EjectRefused(
            409,
            "eject_timeout",
            f"diskutil eject did not answer within {_DISKUTIL_TIMEOUT_S:.0f} s and {root.name} {state}; check before pulling it out",
        ) from exc
    except OSError as exc:
        raise _EjectRefused(409, "eject_failed", f"diskutil eject failed: {exc}") from exc
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        # diskutil's own words carry the useful part ("in use by process ...").
        raise _EjectRefused(409, "eject_failed", stderr or stdout or "diskutil eject failed")
    if root.exists():
        raise _EjectRefused(
            409,
            "eject_unverified",
            f"diskutil reported success but {root} is still mounted",
        )
    return stdout


def eject_after_verified_copy(command: str, root: Path, volume_id: str, verification: object) -> EjectReceipt:
    """Eject the validated stick at ``root`` only after its copy verified clean.

    The verification receipt is checked first (``apps.sync.usb.verify.eject_after_verify``):
    an absent, malformed, failed or other-volume receipt raises
    ``EjectRefused`` before the volume lock, the diskutil probe or the eject
    run. A clean receipt then goes through the same lock, probe and proven
    unmount as ``POST /usb/eject``, and the returned receipt names the volume.
    """

    def _locked_eject(target: Path) -> str:
        with eject_operation(target):
            return _eject_device(command, target, volume_id)

    return eject_after_verify(verification, volume_root=root, volume_identity=volume_id, eject=_locked_eject)


@router.post("/eject", response_model=UsbEjectOut, responses=_ERROR_RESPONSES)
def eject_usb_volume(body: UsbEjectIn, request: Request) -> UsbEjectOut | JSONResponse:
    try:
        try:
            discovery = usb_volumes._system_discovery(usb_export_gate=usb_export_gate(request))
        except usb_volumes.UsbDiscoveryUnavailable as exc:
            raise _EjectRefused(
                503, "usb_eject_unavailable", exc.reason, ui_title=exc.ui_title
            ) from exc
        root = _target_root(body.mount_path, discovery.volumes_root)
        try:
            # Never overlap an export write to this stick or a sibling volume on
            # its disk, which diskutil eject also unmounts (Codex P1 4300896677,
            # 4301299382).
            with eject_operation(root):
                stdout = _eject_device(discovery.diskutil_command, root, body.volume_id)
        except VolumeBusy as exc:
            raise _EjectRefused(
                409,
                "eject_volume_busy",
                f"another USB export or eject is running; eject {root.name} once that finishes",
            ) from exc
    except _EjectRefused as refused:
        return refused.response()
    # Proven gone: the next listing must rescan rather than serve the cached
    # one that still names this stick (Sol P1 4287654317).
    usb_volumes.invalidate_scan_cache()
    return UsbEjectOut(
        mount_path=str(root),
        name=root.name,
        ejected=True,
        message=stdout or f"{root.name} ejected",
    )
