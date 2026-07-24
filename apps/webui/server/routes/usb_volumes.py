"""USB stick volume tracker (read-only detect + classify).

#328 feature: USB stick tracker (detect, name, export health, agentic check)

GET  /api/v1/usb/volumes          - list present volumes (throttled scan)
POST /api/v1/usb/volumes          - inject a simulated volume (DEV / no stick)
GET  /api/v1/usb/volumes/events   - SSE keepalive that refreshes while watched

Never writes to a USB mount. diskutil is optional (fail-soft).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import AsyncIterator, Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)

router = APIRouter(prefix="/usb", tags=["usb"])

VolumeKind = Literal["rekordbox", "djay", "music", "unknown"]
VolumeRole = Literal["usb_stick", "mounted_drive", "disk_image", "other"]

_AUDIO_EXTS = frozenset(
    {".mp3", ".m4a", ".flac", ".wav", ".aiff", ".aif", ".aac", ".ogg", ".alac"}
)
_MIN_SCAN_INTERVAL_S = 5.0
_VOLUMES_ROOT = Path("/Volumes")

# ----- module state (process-local; cheap) ---------------------------------

_last_scan_mono: float = 0.0
_last_client_mono: float = 0.0
_sse_watchers: int = 0
_cached: list["UsbVolume"] = []
_fakes: dict[str, "UsbVolume"] = {}


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


# ----- scan (fail-soft) ---------------------------------------------------


def _touch_client() -> None:
    global _last_client_mono
    _last_client_mono = time.monotonic()


def _someone_watching() -> bool:
    if _sse_watchers > 0:
        return True
    return (time.monotonic() - _last_client_mono) < 30.0


def _scan_volumes(*, force: bool = False) -> list[UsbVolume]:
    """Rescan /Volumes when watched and interval elapsed; else return cache."""
    global _last_scan_mono, _cached
    now = time.monotonic()
    if (
        not force
        and _cached
        and (now - _last_scan_mono) < _MIN_SCAN_INTERVAL_S
    ):
        return _merge_fakes(_cached)
    if not force and not _someone_watching() and _cached:
        # Idle: do not burn diskutil / walk cycles.
        return _merge_fakes(_cached)

    found: list[UsbVolume] = []
    if _VOLUMES_ROOT.is_dir():
        try:
            entries = sorted(_VOLUMES_ROOT.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            log.warning("usb volumes: cannot list %s: %s", _VOLUMES_ROOT, exc)
            entries = []
        for entry in entries:
            if not entry.is_dir():
                continue
            if _skip_volume_name(entry.name):
                continue
            info = _diskutil_info(entry)
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
    return _merge_fakes(found)


def _merge_fakes(real: list[UsbVolume]) -> list[UsbVolume]:
    by_id = {v.id: v for v in real}
    for fake in _fakes.values():
        by_id[fake.id] = fake
    return list(by_id.values())


def _skip_volume_name(name: str) -> bool:
    """Ignore boot / Time Machine / hidden system mounts (not USB sticks)."""
    if name in ("Macintosh HD", "Macintosh HD - Data"):
        return True
    if name.startswith("."):
        return True
    lower = name.lower()
    if "timemachine" in lower or "time machine" in lower:
        return True
    if lower.startswith("com.apple."):
        return True
    return False


def _diskutil_info(mount: Path) -> DiskutilInfo:
    import shutil
    import subprocess

    if shutil.which("diskutil") is None:
        return DiskutilInfo()
    try:
        proc = subprocess.run(
            ["diskutil", "info", "-plist", str(mount)],
            check=False,
            capture_output=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return DiskutilInfo()
    if proc.returncode != 0 or not proc.stdout:
        return DiskutilInfo()
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


@router.get("/volumes", response_model=UsbVolumesOut)
def get_usb_volumes() -> UsbVolumesOut:
    _touch_client()
    vols = _scan_volumes()
    return UsbVolumesOut(
        volumes=[_to_out(v) for v in vols],
        scanned_at=time.time(),
        watching=_someone_watching(),
    )


@router.post("/volumes", response_model=UsbVolumesOut)
def post_usb_volume(body: UsbVolumePost) -> UsbVolumesOut:
    """Inject or update a simulated volume (manual test without a stick)."""
    _touch_client()
    vol_id = body.id or f"sim:{body.name.strip().lower().replace(' ', '-')}"
    reason = hide_reason_for(body.role, protocol=body.protocol, name=body.name)
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
    vols = _scan_volumes(force=True)
    return UsbVolumesOut(
        volumes=[_to_out(v) for v in vols],
        scanned_at=time.time(),
        watching=_someone_watching(),
    )


@router.get("/volumes/events")
async def usb_volume_events(request: Request) -> StreamingResponse:
    """SSE: keep the scanner warm while a client is subscribed."""

    async def _gen() -> AsyncIterator[bytes]:
        global _sse_watchers
        _sse_watchers += 1
        _touch_client()
        try:
            while True:
                if await request.is_disconnected():
                    break
                vols = _scan_volumes()
                payload = {
                    "volumes": [asdict(v) for v in vols],
                    "scanned_at": time.time(),
                }
                yield f"data: {json.dumps(payload)}\n\n".encode("utf-8")
                await asyncio.sleep(_MIN_SCAN_INTERVAL_S)
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
    _fakes = {}
