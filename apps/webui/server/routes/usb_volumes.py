"""USB stick volume tracker (read-only detect + classify).

#328 feature: USB stick tracker (detect, name, export health, agentic check)

GET  /api/v1/usb/volumes          - list present volumes (throttled scan)
GET  /api/v1/usb/volumes/events   - SSE keepalive that refreshes while watched
POST /api/v1/usb/volumes          - simulated volume injection, mounted ONLY
                                    when MDT_USB_SIMULATION=1 (tests / dev
                                    without a stick); production never serves
                                    or merges simulated volumes.

Never writes to a USB mount. Discovery fails explicitly without macOS diskutil.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, field_validator

log = logging.getLogger(__name__)

router = APIRouter(prefix="/usb", tags=["usb"])
simulation_router = APIRouter(prefix="/usb", tags=["usb"])

VolumeKind = Literal["rekordbox", "djay", "music", "unknown"]
VolumeRole = Literal["usb_stick", "mounted_drive", "disk_image", "other"]

_AUDIO_EXTS = frozenset(
    {".mp3", ".m4a", ".flac", ".wav", ".aiff", ".aif", ".aac", ".ogg", ".alac"}
)
_MIN_SCAN_INTERVAL_S = 5.0
_MAX_SIMULATED_VOLUMES = 16
_SIMULATED_ID_PREFIX = "sim:"
_SIMULATION_ENV = "MDT_USB_SIMULATION"
_VOLUMES_ROOT = Path("/Volumes")


def simulation_enabled() -> bool:
    """Explicit opt-in gate for the simulated-volume surface.

    Unset / "0" = disabled (production default). "1" = enabled (tests / dev
    without a stick). Anything else fails fast: a typo must never silently
    disable or enable a test-only surface.
    """
    raw = os.environ.get(_SIMULATION_ENV, "0")
    if raw in ("", "0"):
        return False
    if raw == "1":
        return True
    raise ValueError(f"{_SIMULATION_ENV} must be unset, '0' or '1'; got {raw!r}")


class UsbDiscoveryUnavailable(RuntimeError):
    """The host cannot provide trustworthy USB volume discovery."""

    code = "usb_volume_discovery_unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"USB volume discovery unavailable: {reason}")

    def to_detail(self) -> dict[str, str]:
        return {"code": self.code, "reason": self.reason}


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
    hide_reason: str | None = None


# ----- module state (process-local; cheap) ---------------------------------

_last_scan_mono: float = 0.0
_last_client_mono: float = 0.0
_sse_watchers: int = 0
_cached: list[UsbVolume] = []
_fakes: dict[str, UsbVolume] = {}
_FAKES_LOCK = threading.Lock()


# ----- pure classify (unit-tested) ----------------------------------------


def classify_mount(root: Path) -> VolumeKind:
    """Classify a mounted volume by shallow folder / audio presence.

    Order: Pioneer/rekordbox export layout, djay, any audio files, else unknown.
    Never descends deep - one level for DJ folders, shallow walk for audio.
    """
    if not root.is_dir():
        return "unknown"
    try:
        names = {p.name for p in root.iterdir()}
    except OSError:
        return "unknown"
    lower = {n.lower() for n in names}
    if "pioneer" in lower or "rekordbox" in lower:
        return "rekordbox"
    if "djay" in lower or "djay media library.djaymediadatabase" in lower:
        return "djay"
    if _has_audio_shallow(root, max_entries=80):
        return "music"
    return "unknown"


def classify_role(
    *,
    protocol: str | None,
    removable: bool | None,
    internal: bool | None = None,
) -> VolumeRole:
    """Map diskutil BusProtocol / RemovableMedia / Internal to a volume role.

    Human `diskutil info` prints Removable Media as Fixed/Removable; the plist
    exposes RemovableMedia as a bool (False ~= Fixed, True ~= Removable).
    """
    proto = (protocol or "").strip().lower()
    if "disk image" in proto:
        return "disk_image"
    if "usb" in proto:
        if removable is False:
            return "mounted_drive"
        return "usb_stick"
    if internal is True or removable is False:
        return "mounted_drive"
    if removable is True:
        return "usb_stick"
    return "other"


def hide_reason_for(
    role: VolumeRole,
    *,
    protocol: str | None = None,
    name: str | None = None,
) -> str | None:
    """Human reason tag for non-USB / non-music folds (no U+2014 or U+2013 characters)."""
    if role == "usb_stick":
        return None
    if role == "mounted_drive":
        detail = _short_protocol(protocol) or "mounted drive"
        return f"not-usb(mounted drive - {detail})"
    if role == "disk_image":
        label = _short_name(name) or _short_protocol(protocol) or "disk image"
        return f"not-usb(disk image - {label})"
    detail = _short_protocol(protocol) or "other mount"
    return f"not-usb({detail})"


def _short_protocol(protocol: str | None) -> str | None:
    if not protocol:
        return None
    proto = protocol.strip()
    if not proto:
        return None
    lower = proto.lower()
    if "disk image" in lower:
        return "disk image"
    return proto


def _short_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = name.strip()
    if not cleaned:
        return None
    # Prefer a short alias for known tooling mounts.
    lower = cleaned.lower()
    if "copilot" in lower:
        return "copilot"
    return cleaned


def _has_audio_shallow(root: Path, *, max_entries: int) -> bool:
    seen = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip heavy / system trees; never touch Contents of apps.
            dirnames[:] = [
                d
                for d in dirnames
                if d
                not in (
                    "System Volume Information",
                    ".Spotlight-V100",
                    ".fseventsd",
                    ".Trashes",
                    "$RECYCLE.BIN",
                )
                and not d.startswith(".")
            ]
            for name in filenames:
                seen += 1
                if Path(name).suffix.lower() in _AUDIO_EXTS:
                    return True
                if seen >= max_entries:
                    return False
            if seen >= max_entries:
                return False
            # Cap depth: root + one level of dirs only.
            if Path(dirpath) != root:
                dirnames.clear()
    except OSError:
        return False
    return False


# ----- scan (fail-fast) ---------------------------------------------------


def _resolve_discovery(
    *,
    platform_name: str,
    volumes_root: Path,
    diskutil_command: str | None,
) -> UsbDiscovery:
    if platform_name != "darwin":
        raise UsbDiscoveryUnavailable(f"unsupported_platform:{platform_name}")
    if not volumes_root.is_dir():
        raise UsbDiscoveryUnavailable("volumes_root_unavailable")
    if diskutil_command is None:
        raise UsbDiscoveryUnavailable("diskutil_unavailable")
    return UsbDiscovery(
        volumes_root=volumes_root,
        diskutil_command=diskutil_command,
    )


def _system_discovery() -> UsbDiscovery:
    return _resolve_discovery(
        platform_name=sys.platform,
        volumes_root=_VOLUMES_ROOT,
        diskutil_command=shutil.which("diskutil"),
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
    force: bool = False,
    discovery: UsbDiscovery | None = None,
    include_simulated: bool = False,
) -> list[UsbVolume]:
    """Rescan /Volumes when watched and interval elapsed; else return cache."""
    global _last_scan_mono, _cached
    resolved_discovery = discovery or _system_discovery()
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


def _diskutil_info(mount: Path, diskutil_command: str) -> DiskutilInfo:
    try:
        proc = subprocess.run(
            [diskutil_command, "info", "-plist", str(mount)],
            check=False,
            capture_output=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
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


class UsbVolumePost(BaseModel):
    """DEV / simulate: inject a fake present volume (never touches disk)."""

    model_config = ConfigDict(frozen=True)

    id: str | None = None
    name: str = "FAKE USB"
    mount_path: str | None = "/Volumes/FAKE-USB"
    kind: VolumeKind = "music"
    present: bool = True
    role: VolumeRole = "usb_stick"
    protocol: str | None = "USB"

    @field_validator("id")
    @classmethod
    def _validate_simulated_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        suffix = value.removeprefix(_SIMULATED_ID_PREFIX)
        if (
            not value.startswith(_SIMULATED_ID_PREFIX)
            or not suffix.strip()
            or value != value.strip()
        ):
            raise ValueError(
                "simulated volume id must use the reserved 'sim:' namespace"
            )
        return value


class UsbCapabilityDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: Literal["usb_volume_discovery_unavailable"]
    reason: str


class UsbCapabilityErrorOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    detail: UsbCapabilityDetail


_CAPABILITY_RESPONSES = {
    503: {
        "model": UsbCapabilityErrorOut,
        "description": "USB volume discovery is unavailable on this host.",
    }
}


def _capability_response(exc: UsbDiscoveryUnavailable) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": exc.to_detail()},
    )


def _to_out(v: UsbVolume) -> UsbVolumeOut:
    is_music = v.kind in ("rekordbox", "djay", "music") and v.role == "usb_stick"
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
        vols = _scan_volumes(
            include_simulated=request.app.state.usb_simulation_enabled,
        )
    except UsbDiscoveryUnavailable as exc:
        return _capability_response(exc)
    return UsbVolumesOut(
        volumes=[_to_out(v) for v in vols],
        scanned_at=time.time(),
        watching=_someone_watching(),
    )


@simulation_router.post("/volumes", response_model=UsbVolumesOut)
def post_usb_volume(body: UsbVolumePost) -> UsbVolumesOut:
    """Inject or update bounded simulated state without touching a stick."""
    _touch_client()
    generated_suffix = "-".join(body.name.strip().lower().split()) or "unnamed"
    vol_id = body.id or f"{_SIMULATED_ID_PREFIX}{generated_suffix}"
    reason = hide_reason_for(body.role, protocol=body.protocol, name=body.name)
    if any(volume.id == vol_id for volume in _cached):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "usb_simulation_id_collision",
                "reason": "real_volume_id_reserved",
            },
        )
    with _FAKES_LOCK:
        if vol_id not in _fakes and len(_fakes) >= _MAX_SIMULATED_VOLUMES:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "usb_simulation_capacity_exceeded",
                    "reason": "simulated_volume_limit_reached",
                },
            )
        _fakes[vol_id] = UsbVolume(
            id=vol_id,
            name=body.name,
            mount_path=body.mount_path,
            kind=body.kind,
            present=body.present,
            simulated=True,
            role=body.role,
            protocol=body.protocol,
            hide_reason=reason,
        )
    vols = _with_simulations(_cached)
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
        discovery = _system_discovery()
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
