"""Drive marker: ``/<drive>/.mdj-marker.json``.

The marker is a tiny JSON sidecar that records the last apply run so we
can detect drive-identity drift (same label, different UUID) and report
the last-applied plan.
"""
from __future__ import annotations

import datetime as _dt
import json
import subprocess
from pathlib import Path
from typing import Any

from apps.shared import macos_diskutil

MARKER_NAME = ".mdj-marker.json"


def _volume_uuid(drive_root: Path) -> str | None:
    """Best-effort: return ``diskutil info`` VolumeUUID for ``drive_root``.

    Returns None on any failure (tests, non-macOS hosts, etc.).
    """
    try:
        proc = subprocess.run(
            [str(macos_diskutil.DISKUTIL_PATH), "info", "-plist", str(drive_root)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    # Plist parsing with stdlib.
    try:
        import plistlib

        info: dict[str, Any] = plistlib.loads(proc.stdout.encode("utf-8"))
    except Exception:
        return None
    return info.get("VolumeUUID")


def read_marker(drive_root: Path) -> dict[str, Any] | None:
    path = drive_root / MARKER_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_marker(
    drive_root: Path,
    *,
    profile_name: str,
    drive_uuid: str | None,
    reversal_log: Path | None,
    plan_summary: dict[str, int],
) -> Path:
    path = drive_root / MARKER_NAME
    payload: dict[str, Any] = {
        "profile_name": profile_name,
        "drive_uuid": drive_uuid,
        "applied_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "reversal_log": str(reversal_log) if reversal_log else None,
        "plan_summary": plan_summary,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def resolve_drive_uuid(
    drive_root: Path,
    *,
    existing_marker: dict[str, Any] | None = None,
) -> str | None:
    """Resolve the drive's UUID, preferring the live probe, then the marker."""
    live = _volume_uuid(drive_root)
    if live:
        return live
    if existing_marker and existing_marker.get("drive_uuid"):
        return existing_marker["drive_uuid"]
    return None


__all__ = [
    "MARKER_NAME",
    "read_marker",
    "write_marker",
    "resolve_drive_uuid",
]
