"""Practice replica panel layouts (hardware layout skins, ADR-0137).

GET /api/v1/practice/devices            - devices that ship a panel layout
GET /api/v1/practice/devices/{device}   - one panel layout document (opendj-panel-layout/1)

Repo content, not user data: layouts live at
``tools/deck-diagrams/devices/<device>/panel.json``, rooted at the checkout like the
offline MIDI catalog in midi_maps.py. The document is served verbatim; the frontend joins
each control to the shipped DeviceMap by wire identity, so this route never carries actions.

Errors:
  404 PRACTICE_DEVICE_NOT_FOUND   no panel layout for that device id
  422 PRACTICE_LAYOUT_INVALID     the file on disk is not a panel layout v1 document
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from apps.shared.platform_paths import PROJECT_ROOT

router = APIRouter(prefix="/practice", tags=["practice"])
MOUNT_UNDER_API_PREFIX = True

DEVICES_DIR = PROJECT_ROOT / "tools" / "deck-diagrams" / "devices"
PANEL_FORMAT = "opendj-panel-layout/1"
_DEVICE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _panel_path(device: str) -> Path:
    return DEVICES_DIR / device / "panel.json"


def _load_panel(device: str) -> dict[str, Any]:
    path = _panel_path(device)
    if not _DEVICE_ID.match(device) or not path.is_file():
        raise HTTPException(404, {"code": "PRACTICE_DEVICE_NOT_FOUND", "message": f"no panel layout for {device!r}"})
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("format") != PANEL_FORMAT or doc.get("device") != device or not doc.get("controls"):
        raise HTTPException(
            422, {"code": "PRACTICE_LAYOUT_INVALID", "message": f"{path.name} for {device!r} is not {PANEL_FORMAT}"}
        )
    return doc


@router.get("/devices")
def list_practice_devices() -> dict[str, list[str]]:
    return {"devices": sorted(p.parent.name for p in DEVICES_DIR.glob("*/panel.json"))}


@router.get("/devices/{device}")
def get_practice_device(device: str) -> dict[str, Any]:
    return _load_panel(device)
