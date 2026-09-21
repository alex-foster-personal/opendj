"""USB stick volume tracker (read-only detect + classify).

#328 feature: USB stick tracker (detect, name, export health, agentic check)

GET  /api/v1/usb/volumes          - list present volumes (throttled scan)
GET  /api/v1/usb/volumes/events   - SSE keepalive that refreshes while watched

The simulated-volume POST lives in usb_volumes_sim and is mounted ONLY when
MDT_USB_SIMULATION=1; listings here merge simulated rows only under that
gate, so production never serves or merges simulated volumes.

Never writes to a USB mount. Discovery fails explicitly without macOS diskutil.

SANDBOXED, IT REFUSES (SAND-01, SAND-02). A process inside the macOS App
Sandbox may not list /Volumes at all, and the failure is silent: the listing
comes back empty, which reads to a user as "nothing is plugged in" rather
than "this build cannot see drives". Discovery therefore asks
:func:`apps.shared.sandbox.is_sandboxed` FIRST and answers 503 carrying the
fourth refusal sentence, so the panel says which fact it is.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from apps.shared.sandbox import STORE_BUILD_REFUSAL_TITLE, is_sandboxed
from apps.webui.server.routes.usb_classify import (
    VolumeKind,
    VolumeRole,
    classify_mount,
    classify_role,
    hide_reason_for,
)
from apps.webui.server.routes.usb_gate import UsbExportGate, usb_export_gate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/usb", tags=["usb"])

_MIN_SCAN_INTERVAL_S = 5.0
_VOLUMES_ROOT = Path("/Volumes")


class UsbDiscoveryUnavailable(RuntimeError):
    """The host cannot provide trustworthy USB volume discovery."""

    code = "usb_volume_discovery_unavailable"

    def __init__(self, reason: str, *, ui_title: str | None = None) -> None:
        self.reason = reason
        #: The sentence a dead control should carry, when one of the four
        #: refusal states applies. None for a plain host fault, which is not
        #: a refusal: "diskutil is missing" is a broken machine, not a build
        #: that may not look.
        self.ui_title = ui_title
        super().__init__(f"USB volume discovery unavailable: {reason}")

    def to_detail(self) -> dict[str, str]:
        detail = {"code": self.code, "reason": self.reason}
        if self.ui_title is not None:
            detail["ui_title"] = self.ui_title
        return detail


@dataclass(frozen=True)
class UsbDiscovery:
    volumes_root: Path
    diskutil_command: str


@dataclass(frozen=True)
class DiskutilInfo:
    volume_uuid: str | None = None
    protocol: str | None = None
    removable: bool | None = None
    internal: bool | None = None


@dataclass(frozen=True)
class UsbVolume:
    id: str
    name: str
    mount_path: str | None
    kind: VolumeKind
    present: bool = True
    simulated: bool = False
    role: VolumeRole = "other"
    protocol: str | None = None
    removable: bool | None = None
    hide_reason: str | None = None


# ----- module state (process-local; cheap) ---------------------------------

_last_scan_mono: float = 0.0
_last_client_mono: float = 0.0
_sse_watchers: int = 0
_cached: list[UsbVolume] = []
_fakes: dict[str, UsbVolume] = {}
_FAKES_LOCK = threading.Lock()


# ----- pure classify ------------------------------------------------------
#
# Moved to usb_classify: a total function of its arguments, with no cache, no
# subprocess and no route, is testable without a mounted drive, and this file
# was at the 600-line ceiling. Re-exported so `usb_volumes.classify_mount`
# keeps working for the scanner, usb_volumes_sim and the existing tests.


# ----- scan (fail-fast) ---------------------------------------------------


def _resolve_discovery(
    *,
    platform_name: str,
    volumes_root: Path,
    diskutil_command: str | None,
    sandboxed: bool | None = None,
    usb_export_gate: UsbExportGate,
) -> UsbDiscovery:
    # SAND-01. Checked before even the platform, because a build that
    # declared usb.export off (the shipped appstore profile) is not
    # available regardless of what host it happens to run on -- a developer
    # testing that profile on Linux must see the same refusal a real store
    # build would, not "unsupported platform". The ui_title comes from the
    # gate's own refusal, which is None (a neutral disablement, not blamed on
    # Apple) unless the store profile itself is the reason (SAND-01 review
    # round 2, PR #1668).
    if not usb_export_gate.flag_enabled:
        raise UsbDiscoveryUnavailable(
            "usb_export_disabled_in_this_build",
            ui_title=(
                usb_export_gate.refusal.ui_title
                if usb_export_gate.refusal is not None
                else None
            ),
        )
    if platform_name != "darwin":
        raise UsbDiscoveryUnavailable(f"unsupported_platform:{platform_name}")
    # SAND-01/SAND-02, and it goes BEFORE the volumes_root probe on purpose.
    # A sandboxed process is not permitted to list /Volumes, so is_dir() is
    # False and the next branch would answer "volumes_root_unavailable" --
    # which reads as "this Mac has no /Volumes", a fault of the machine. The
    # truth is a build that may not look, and those are different sentences
    # to a user and different next steps to an agent.
    if is_sandboxed() if sandboxed is None else sandboxed:
        raise UsbDiscoveryUnavailable(
            "app_sandbox_forbids_volume_listing",
            ui_title=STORE_BUILD_REFUSAL_TITLE,
        )
    if not volumes_root.is_dir():
        raise UsbDiscoveryUnavailable("volumes_root_unavailable")
    if diskutil_command is None:
        raise UsbDiscoveryUnavailable("diskutil_unavailable")
    return UsbDiscovery(
        volumes_root=volumes_root,
        diskutil_command=diskutil_command,
    )


def _system_discovery(*, usb_export_gate: UsbExportGate) -> UsbDiscovery:
    return _resolve_discovery(
        platform_name=sys.platform,
        volumes_root=_VOLUMES_ROOT,
        diskutil_command=shutil.which("diskutil"),
        usb_export_gate=usb_export_gate,
    )


def _touch_client() -> None:
    global _last_client_mono
    _last_client_mono = time.monotonic()


def _someone_watching() -> bool:
    if _sse_watchers > 0:
        return True
    return (time.monotonic() - _last_client_mono) < 30.0


def _scan_volumes(
    *,
    discovery: UsbDiscovery,
    force: bool = False,
    include_simulated: bool = False,
) -> list[UsbVolume]:
    """Rescan /Volumes when watched and interval elapsed; else return cache."""
    global _last_scan_mono, _cached
    resolved_discovery = discovery
    now = time.monotonic()
    if (
        not force
        and _cached
        and (now - _last_scan_mono) < _MIN_SCAN_INTERVAL_S
    ):
        return _with_simulations(_cached) if include_simulated else list(_cached)
    if not force and not _someone_watching() and _cached:
        # Idle: do not burn diskutil / walk cycles.
        return _with_simulations(_cached) if include_simulated else list(_cached)

    found: list[UsbVolume] = []
    try:
        entries = sorted(
            resolved_discovery.volumes_root.iterdir(),
            key=lambda path: path.name.lower(),
        )
    except OSError as exc:
        log.warning(
            "usb volumes: cannot list %s: %s",
            resolved_discovery.volumes_root,
            exc,
        )
        raise UsbDiscoveryUnavailable("volumes_root_unreadable") from exc
    for entry in entries:
        if not entry.is_dir():
            continue
        if _skip_volume_name(entry.name):
            continue
        info = _diskutil_info(entry, resolved_discovery.diskutil_command)
        role = classify_role(
            protocol=info.protocol,
            removable=info.removable,
            internal=info.internal,
        )
        # Never shallow-walk huge Fixed / disk-image mounts (hang risk).
        if role in ("mounted_drive", "disk_image"):
            kind: VolumeKind = "unknown"
        else:
            kind = classify_mount(entry)
        vol_id = (
            f"vol:{info.volume_uuid}" if info.volume_uuid else f"path:{entry.name}"
        )
        found.append(
            UsbVolume(
                id=vol_id,
                name=entry.name,
                mount_path=str(entry),
                kind=kind,
                present=True,
                simulated=False,
                role=role,
                protocol=info.protocol,
                removable=info.removable,
                hide_reason=hide_reason_for(
                    role, protocol=info.protocol, name=entry.name
                ),
            )
        )
    _cached = found
    _last_scan_mono = now
    return _with_simulations(found) if include_simulated else found


def _with_simulations(real: list[UsbVolume]) -> list[UsbVolume]:
    real_ids = {volume.id for volume in real}
    with _FAKES_LOCK:
        non_colliding_fakes = [
            fake for fake in _fakes.values() if fake.id not in real_ids
        ]
    return [*real, *non_colliding_fakes]


def _skip_volume_name(name: str) -> bool:
    """Ignore boot / Time Machine / hidden system mounts (not USB sticks)."""
    if name in ("Macintosh HD", "Macintosh HD - Data"):
        return True
    if name.startswith("."):
        return True
    lower = name.lower()
    if "timemachine" in lower or "time machine" in lower:
        return True
    return lower.startswith("com.apple.")


# diskutil talks to diskarbitrationd and can block behind a spinning-up or
# slow-to-answer device. 2.0s was too tight to be a fault signal: on a loaded
# machine (a full test suite, a build) it timed out on the healthy internal
# disk, turned the whole route into a 503, and made
# tests/webui/test_usb_volumes.py::test_get_volumes_ok flaky. Measured 1 in 5
# failures locally under load, Mon 31 Aug 2026. 10s is still bounded, so a
# genuinely wedged diskutil is still reported rather than hanging the request.
_DISKUTIL_TIMEOUT_S: float = 10.0


def _diskutil_info(mount: Path, diskutil_command: str) -> DiskutilInfo:
    try:
        proc = subprocess.run(
            [diskutil_command, "info", "-plist", str(mount)],
            check=False,
            capture_output=True,
            timeout=_DISKUTIL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        # Distinguished from a hard failure on purpose: "diskutil did not
        # answer in 10s" and "diskutil returned an error" are different
        # faults, and collapsing them cost a debugging session here.
        raise UsbDiscoveryUnavailable("diskutil_timed_out") from exc
    except OSError as exc:
        raise UsbDiscoveryUnavailable("diskutil_query_failed") from exc
    if proc.returncode != 0 or not proc.stdout:
        raise UsbDiscoveryUnavailable("diskutil_query_failed")
    text = proc.stdout.decode("utf-8", errors="replace")
    return DiskutilInfo(
        volume_uuid=_plist_string(text, "VolumeUUID"),
        protocol=_plist_string(text, "BusProtocol") or _plist_string(text, "Protocol"),
        removable=_plist_bool(text, "RemovableMedia")
        if _plist_bool(text, "RemovableMedia") is not None
        else _plist_bool(text, "Removable"),
        internal=_plist_bool(text, "Internal"),
    )


def _plist_string(text: str, key: str) -> str | None:
    marker = f"<key>{key}</key>"
    idx = text.find(marker)
    if idx < 0:
        return None
    rest = text[idx + len(marker) :]
    start = rest.find("<string>")
    end = rest.find("</string>")
    if start < 0 or end < 0 or end <= start:
        return None
    # Guard: next tag must be the string (skip bools / nested keys).
    between = rest[:start].strip()
    if between and not between.startswith("<"):
        return None
    if "<key>" in rest[:start]:
        return None
    value = rest[start + len("<string>") : end].strip()
    return value or None


def _plist_bool(text: str, key: str) -> bool | None:
    marker = f"<key>{key}</key>"
    idx = text.find(marker)
    if idx < 0:
        return None
    rest = text[idx + len(marker) :].lstrip()
    if rest.startswith("<true"):
        return True
    if rest.startswith("<false"):
        return False
    return None


# ----- wire models --------------------------------------------------------


class UsbVolumeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    mount_path: str | None = None
    kind: VolumeKind = "unknown"
    present: bool = True
    simulated: bool = False
    is_music: bool = False
    role: VolumeRole = "other"
    protocol: str | None = None
    hide_reason: str | None = None


class UsbVolumesOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    volumes: list[UsbVolumeOut]
    scanned_at: float
    watching: bool


class UsbCapabilityDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: Literal["usb_volume_discovery_unavailable"]
    reason: str
    #: Present only when the refusal is one a control should SHOW: today the
    #: App Store build's fourth refusal (SAND-01). Absent for a host fault,
    #: where the honest UI is an error and not a refusal.
    ui_title: str | None = None


class UsbCapabilityErrorOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    detail: UsbCapabilityDetail


_CAPABILITY_RESPONSES = {
    503: {
        "model": UsbCapabilityErrorOut,
        "description": (
            "USB volume discovery is unavailable: either this host cannot "
            "provide it, or this build is sandboxed and may not look."
        ),
    }
}


def _capability_response(exc: UsbDiscoveryUnavailable) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": exc.to_detail()},
    )


def _to_out(v: UsbVolume) -> UsbVolumeOut:
    kind_is_music = v.kind in ("rekordbox", "djay", "music")
    # role == "other" with no protocol/removable at all means diskutil gave us
    # nothing to classify with (unavailable, timed out on a read that still
    # returned some bytes, or a plist missing both keys) rather than a real
    # non-usb bus we deliberately fold away. classify_mount() still proved
    # music content in that case, so trust it instead of hiding a genuine USB
    # music volume behind a metadata gap.
    metadata_unavailable = (
        v.role == "other" and v.protocol is None and v.removable is None
    )
    is_music = kind_is_music and (v.role == "usb_stick" or metadata_unavailable)
    return UsbVolumeOut(
        id=v.id,
        name=v.name,
        mount_path=v.mount_path,
        kind=v.kind,
        present=v.present,
        simulated=v.simulated,
        is_music=is_music,
        role=v.role,
        protocol=v.protocol,
        hide_reason=v.hide_reason
        if not is_music
        else None,
    )


# ----- routes -------------------------------------------------------------


@router.get(
    "/volumes",
    response_model=UsbVolumesOut,
    responses=_CAPABILITY_RESPONSES,
)
def get_usb_volumes(request: Request) -> UsbVolumesOut | JSONResponse:
    _touch_client()
    try:
        discovery = _system_discovery(usb_export_gate=usb_export_gate(request))
        vols = _scan_volumes(
            discovery=discovery,
            include_simulated=request.app.state.usb_simulation_enabled,
        )
    except UsbDiscoveryUnavailable as exc:
        return _capability_response(exc)
    return UsbVolumesOut(
        volumes=[_to_out(v) for v in vols],
        scanned_at=time.time(),
        watching=_someone_watching(),
    )


@router.get(
    "/volumes/events",
    response_class=StreamingResponse,
    response_model=None,
    responses={
        200: {
            "description": "Server-sent USB volume discovery events.",
            "content": {
                "text/event-stream": {"schema": {"type": "string"}},
            },
        },
        **_CAPABILITY_RESPONSES,
    },
)
async def usb_volume_events(request: Request) -> StreamingResponse | JSONResponse:
    """SSE: keep the scanner warm while a client is subscribed."""

    try:
        discovery = _system_discovery(usb_export_gate=usb_export_gate(request))
        include_simulated = request.app.state.usb_simulation_enabled
        initial_volumes = _scan_volumes(
            discovery=discovery,
            include_simulated=include_simulated,
        )
    except UsbDiscoveryUnavailable as exc:
        return _capability_response(exc)

    async def _gen() -> AsyncIterator[bytes]:
        global _sse_watchers
        _sse_watchers += 1
        _touch_client()
        volumes = initial_volumes
        try:
            while True:
                if await request.is_disconnected():
                    break
                payload = {
                    "volumes": [asdict(volume) for volume in volumes],
                    "scanned_at": time.time(),
                }
                yield f"data: {json.dumps(payload)}\n\n".encode()
                await asyncio.sleep(_MIN_SCAN_INTERVAL_S)
                volumes = _scan_volumes(
                    discovery=discovery,
                    include_simulated=include_simulated,
                )
        finally:
            _sse_watchers = max(0, _sse_watchers - 1)

    return StreamingResponse(_gen(), media_type="text/event-stream")


# ----- test helpers -------------------------------------------------------


def _reset_state_for_tests() -> None:
    global _last_scan_mono, _last_client_mono, _sse_watchers, _cached, _fakes
    _last_scan_mono = 0.0
    _last_client_mono = 0.0
    _sse_watchers = 0
    _cached = []
    with _FAKES_LOCK:
        _fakes = {}
