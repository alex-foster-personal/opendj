"""Read-only vendor USB stick names for GET /api/v1/usb/volumes (#328, #11166).

Merges Rekordbox ``djmdDevice`` rows and djay library mentions onto live volume
rows by VolumeUUID, rekordbox device id, or mount label. Never writes to USB or
vendor databases; a missing or unreadable vendor DB is logged and ignored.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from apps.adapters.djay.library import list_djay_usb_mentions
from apps.adapters.rekordbox.models import VendorUsbMention, VendorUsbSource
from apps.adapters.rekordbox.paths import list_rekordbox_usb_mentions
from apps.shared import platform_paths

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VendorUsbName:
    name: str
    source: VendorUsbSource


@dataclass
class VendorUsbHistory:
    by_uuid: dict[str, VendorUsbName]
    by_device_id: dict[str, VendorUsbName]
    by_mount_name: dict[str, VendorUsbName]


def _ingest(mentions: list[VendorUsbMention], into: VendorUsbHistory) -> None:
    for mention in mentions:
        entry = VendorUsbName(name=mention.name, source=mention.source)
        if mention.volume_uuid is not None:
            into.by_uuid.setdefault(mention.volume_uuid, entry)
        if mention.device_id is not None:
            into.by_device_id.setdefault(mention.device_id, entry)
        into.by_mount_name.setdefault(mention.name.casefold(), entry)


def load_vendor_usb_history(*, data_dir: Path) -> VendorUsbHistory:
    """Best-effort vendor history; always returns a (maybe empty) index."""
    history = VendorUsbHistory(by_uuid={}, by_device_id={}, by_mount_name={})
    master = data_dir / "master.plain.db"
    if master.is_file():
        try:
            _ingest(list_rekordbox_usb_mentions(master), history)
        except (OSError, sqlite3.Error) as exc:
            log.warning("usb vendor history: rekordbox read failed: %s", exc)
    else:
        log.info("usb vendor history: no rekordbox plain db at %s", master.name)

    djay_db = platform_paths.DJAY_LIVE_DB
    if djay_db.is_file():
        try:
            _ingest(list_djay_usb_mentions(djay_db), history)
        except (OSError, sqlite3.Error) as exc:
            log.warning("usb vendor history: djay read failed: %s", exc)
    else:
        log.info("usb vendor history: no djay MediaLibrary.db on this host")

    return history


def lookup_vendor_name(
    history: VendorUsbHistory,
    *,
    volume_id: str,
    volume_uuid: str | None,
    mount_name: str,
) -> VendorUsbName | None:
    if volume_uuid is not None:
        hit = history.by_uuid.get(volume_uuid.upper())
        if hit is not None:
            return hit
    device_key = volume_id.removeprefix("rbdev:")
    if device_key != volume_id:
        hit = history.by_device_id.get(device_key)
        if hit is not None:
            return hit
    return history.by_mount_name.get(mount_name.casefold())
