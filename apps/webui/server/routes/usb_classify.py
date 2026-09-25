"""Deciding WHAT a mounted volume is, as pure functions over a path.

Split out of ``usb_volumes`` because these answer a different question from
the rest of that module: everything here is a total function of its arguments
with no scan cache, no diskutil subprocess, no route and no module state, and
that is what makes them unit-testable without a mounted drive. The scanner
calls them; ``usb_volumes_sim`` calls one of them directly; neither needs the
other's machinery.

Nothing here decides whether a volume may be LOOKED at -- that is the
sandbox refusal in ``usb_volumes._resolve_discovery`` (SAND-01/SAND-02), and
it happens before any of this runs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

VolumeKind = Literal["rekordbox", "djay", "music", "unknown"]
VolumeRole = Literal["usb_stick", "mounted_drive", "disk_image", "other"]

_AUDIO_EXTS = frozenset(
    {".mp3", ".m4a", ".flac", ".wav", ".aiff", ".aif", ".aac", ".ogg", ".alac"}
)


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
    dj_kind = _dj_export_kind_from_names(names)
    if dj_kind is not None:
        return dj_kind
    if _has_audio_shallow(root, max_entries=80):
        return "music"
    return "unknown"


def classify_role(
    *,
    protocol: str | None,
    removable: bool | None,
    has_dj_export: bool,
    internal: bool | None = None,
) -> VolumeRole:
    """Map diskutil BusProtocol / RemovableMedia / Internal to a volume role.

    Human `diskutil info` prints Removable Media as Fixed/Removable; the plist
    exposes RemovableMedia as a bool (False ~= Fixed, True ~= Removable).

    A USB SSD reports Fixed exactly like a USB backup disk, so Fixed alone
    cannot tell them apart. ``has_dj_export`` (from :func:`has_dj_export_at_root`)
    is what does: a Fixed USB volume carrying a PIONEER / djay export at its
    root is a DJ stick, anything else Fixed on USB stays a mounted drive.
    """
    proto = (protocol or "").strip().lower()
    if "disk image" in proto:
        return "disk_image"
    if "usb" in proto:
        if removable is False and not has_dj_export:
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
    """Human reason tag for non-USB / non-music folds (no U+2013/U+2014 characters)."""
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


def has_dj_export_at_root(root: Path) -> bool:
    """True when the volume root holds a PIONEER / rekordbox / djay export.

    One directory listing of the root, never a walk, so it is safe on a large
    Fixed drive where :func:`classify_mount` must not descend.
    """
    try:
        names = {p.name for p in root.iterdir()}
    except OSError:
        return False
    return _dj_export_kind_from_names(names) is not None


def _dj_export_kind_from_names(names: set[str]) -> VolumeKind | None:
    lower = {n.lower() for n in names}
    if "pioneer" in lower or "rekordbox" in lower:
        return "rekordbox"
    if "djay" in lower or "djay media library.djaymediadatabase" in lower:
        return "djay"
    return None


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


__all__ = [
    "VolumeKind",
    "VolumeRole",
    "classify_mount",
    "classify_role",
    "has_dj_export_at_root",
    "hide_reason_for",
]
