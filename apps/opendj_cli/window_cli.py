"""``opendj window``: saved shell geometry (WINSTATE-01, SECONDSCREEN-11715) and extra windows.

    opendj window state     print each window's saved geometry, per monitor
    opendj window reset     forget it; the next launch opens at the default size
    opendj window open|close|focus second-screen
                            post a window command on the order bus and
                            confirm the order result

The Tauri shell writes ``<app data dir>/window-state.json`` on every move or
resize (``apps/desktop/src-tauri/src/window_state.rs``), keyed by window label
(``main`` and ``second-screen``) and then by monitor. A version 1 file holds
only the main window and is reported under ``main``. This verb reads the
file directly, so it works whether or not the app is running. ``reset`` while
the app is open is undone by the next move or resize of its window; quit the
app first. The data dir is the one beside the engine lock: ``--lock``, else
``$OPENDJ_LIVE_LOCK_PATH``, else the packaged app's.

``open``, ``close`` and ``focus`` go through the performance page, which talks
to the desktop shell. A browser tab refuses them with a reason that names the
shell. An unknown label is refused with the known list.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED
from apps.opendj_cli.client import (
    ORDER_TIMEOUT_S,
    EngineClient,
    MalformedResult,
    NoPerformancePage,
    OrderRejected,
    OrderTimedOut,
)
from apps.opendj_cli.confirm import SETTLE_TIMEOUT_S
from apps.opendj_cli.orders import single
from apps.opendj_cli.origin import EngineIdentityMismatch, EngineNotRunning, resolve_origin
from apps.opendj_cli.refusal import fail as _fail
from apps.opendj_cli.verbs import InvocationError, parse_invocation
from apps.shared.engine_data_dir import EngineDataDirError
from apps.shared.engine_origin import LOCK_PATH_ENV, default_lock_path

WINDOW_STATE_FILE = "window-state.json"
WINDOW_LABELS = ("main", "second-screen")
USAGE = "usage: opendj window state | reset | open|close|focus <label>"
WINDOW_ACTIONS = ("open", "close", "focus")
KNOWN_LABELS = ("second-screen",)


def window_state_path(lock: Path | None) -> Path:
    """The shell's window-state file, beside the lock the rest of the CLI reads."""
    override = os.environ.get(LOCK_PATH_ENV, "").strip()
    lock_path = lock if lock is not None else Path(override) if override else default_lock_path()
    return lock_path.parent / WINDOW_STATE_FILE


def read_state(path: Path) -> dict[str, Any]:
    """The saved document, or an empty one marked ``saved: false`` before the first save."""
    windows: dict[str, Any] = {label: {"last_monitor": None, "monitors": {}} for label in WINDOW_LABELS}
    if not path.exists():
        return {"path": str(path), "saved": False, "version": None, "windows": windows}
    document = json.loads(path.read_text(encoding="utf-8"))
    if "windows" in document:
        for label, state in document["windows"].items():
            windows[label] = {"last_monitor": state.get("last_monitor"), "monitors": state.get("monitors", {})}
    else:
        windows["main"] = {"last_monitor": document.get("last_monitor"), "monitors": document.get("monitors", {})}
    return {"path": str(path), "saved": True, "version": document.get("version"), "windows": windows}


def reset_state(path: Path) -> dict[str, Any]:
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"path": str(path), "reset": True, "existed": existed}


def unknown_label_message(label: str) -> str:
    return f"unknown window label {label!r}; known: {', '.join(KNOWN_LABELS)}"


def _text(payload: dict[str, Any]) -> str:
    if "reset" in payload:
        return f"window state reset ({'removed' if payload['existed'] else 'nothing saved'}): {payload['path']}"
    if "action" in payload:
        return f"{payload['action']} {payload['label']}"
    lines = [f"{payload['path']}{'' if payload['saved'] else ' (nothing saved yet)'}"]
    for label, state in payload["windows"].items():
        lines.append(f"{label}:")
        if not state["monitors"]:
            lines.append("  (nothing saved)")
        for key, geometry in state["monitors"].items():
            marker = "*" if key == state["last_monitor"] else " "
            lines.append(
                f"  {marker} {key}: {geometry['width_pt']:.0f}x{geometry['height_pt']:.0f} pt at "
                f"+{geometry['x_offset_pt']:.0f}+{geometry['y_offset_pt']:.0f}"
                f"{' maximized' if geometry['maximized'] else ''}"
            )
    return "\n".join(lines)


def _step_error(result: dict[str, Any]) -> str | None:
    steps = result.get("steps")
    if not isinstance(steps, list):
        return "window command returned no steps"
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("status") == "failed":
            error = step.get("error")
            return str(error) if error else "window command failed"
    return None


def _run_order(
    rest: Sequence[str],
    *,
    as_json: bool,
    lock: Path | None,
    timeout_s: float,
) -> int:
    if len(rest) != 2 or rest[0] not in WINDOW_ACTIONS:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    action, label = rest
    if label not in KNOWN_LABELS:
        return _fail(as_json, "unknown_window_label", unknown_label_message(label), EXIT_FAILED)
    try:
        origin = resolve_origin(lock)
        client = EngineClient(origin=origin, timeout_s=timeout_s)
        result = client.post_order(single(parse_invocation(["window", action, label])))
        error = _step_error(result)
        if error is not None:
            return _fail(as_json, "window_command_failed", error, EXIT_FAILED)
    except (
        InvocationError,
        EngineNotRunning,
        EngineIdentityMismatch,
        NoPerformancePage,
        OrderRejected,
        OrderTimedOut,
        MalformedResult,
    ) as error:
        code = {
            InvocationError: "usage",
            EngineNotRunning: "engine_unavailable",
            EngineIdentityMismatch: "engine_unavailable",
            NoPerformancePage: "no_performance_page",
            OrderRejected: "order_rejected",
            OrderTimedOut: "order_timeout",
            MalformedResult: "malformed_result",
        }[type(error)]
        return _fail(as_json, code, str(error), EXIT_FAILED)
    payload = {"action": action, "label": label}
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else _text(payload))
    return EXIT_CONFIRMED


def run(
    rest: Sequence[str],
    *,
    as_json: bool = False,
    lock: Path | None = None,
    timeout_s: float = ORDER_TIMEOUT_S,
    settle_s: float = SETTLE_TIMEOUT_S,
) -> int:
    # settle_s is kept so `opendj --settle` still parses; confirmation is the
    # order result, not a UI-mirror poll of a local echo.
    _ = settle_s
    if rest and rest[0] in WINDOW_ACTIONS:
        return _run_order(rest, as_json=as_json, lock=lock, timeout_s=timeout_s)
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
