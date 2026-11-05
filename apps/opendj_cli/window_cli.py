"""``opendj window``: the desktop shell's saved window geometry (WINSTATE-01).

    opendj window state     print every monitor's saved geometry
    opendj window reset     forget it; the next launch opens at the default size

The Tauri shell writes ``<app data dir>/window-state.json`` on every move or
resize (``apps/desktop/src-tauri/src/window_state.rs``). This verb reads the
file directly, so it works whether or not the app is running. ``reset`` while
the app is open is undone by the next move or resize of its window; quit the
app first. The data dir is the one beside the engine lock: ``--lock``, else
``$OPENDJ_LIVE_LOCK_PATH``, else the packaged app's.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED
from apps.opendj_cli.refusal import fail as _fail
from apps.shared.engine_data_dir import EngineDataDirError
from apps.shared.engine_origin import LOCK_PATH_ENV, default_lock_path

WINDOW_STATE_FILE = "window-state.json"
USAGE = "usage: opendj window state | opendj window reset"


def window_state_path(lock: Path | None) -> Path:
    """The shell's window-state file, beside the lock the rest of the CLI reads."""
    override = os.environ.get(LOCK_PATH_ENV, "").strip()
    lock_path = lock if lock is not None else Path(override) if override else default_lock_path()
    return lock_path.parent / WINDOW_STATE_FILE


def read_state(path: Path) -> dict[str, Any]:
    """The saved document, or an empty one marked ``saved: false`` before the first save."""
    if not path.exists():
        return {"path": str(path), "saved": False, "last_monitor": None, "monitors": {}}
    document = json.loads(path.read_text(encoding="utf-8"))
    return {"path": str(path), "saved": True, **document}


def reset_state(path: Path) -> dict[str, Any]:
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"path": str(path), "reset": True, "existed": existed}


def _text(payload: dict[str, Any]) -> str:
    if "reset" in payload:
        return f"window state reset ({'removed' if payload['existed'] else 'nothing saved'}): {payload['path']}"
    lines = [f"{payload['path']}{'' if payload['saved'] else ' (nothing saved yet)'}"]
    for key, geometry in payload["monitors"].items():
        marker = "*" if key == payload["last_monitor"] else " "
        lines.append(
            f"{marker} {key}: {geometry['width_pt']:.0f}x{geometry['height_pt']:.0f} pt at "
            f"+{geometry['x_offset_pt']:.0f}+{geometry['y_offset_pt']:.0f}"
            f"{' maximized' if geometry['maximized'] else ''}"
        )
    return "\n".join(lines)


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    if len(rest) != 1 or rest[0] not in ("state", "reset"):
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    try:
        path = window_state_path(lock)
        payload = read_state(path) if rest[0] == "state" else reset_state(path)
    except EngineDataDirError as error:
        return _fail(as_json, "data_dir_unresolved", str(error), EXIT_FAILED)
    except (OSError, json.JSONDecodeError) as error:
        return _fail(as_json, "window_state_unreadable", f"{error}", EXIT_FAILED)
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else _text(payload))
    return EXIT_CONFIRMED
