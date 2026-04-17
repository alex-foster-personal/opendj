"""Actuator for the Claude computer-use Rekordbox export agent.

Responsibilities
----------------

* Capture the macOS main display via Quartz and return downsampled PNG bytes
  suitable for Claude's vision input (≤ `downsample_width` pixels wide).
* Dispatch Claude-style computer-use actions (``left_click``, ``type``,
  ``screenshot``, etc.) through ``cliclick`` — a tiny CLI that drives the
  macOS HID subsystem correctly on Retina displays (unlike ``pyautogui``
  which has a longstanding coordinate-scaling bug there).
* Maintain an append-only trace directory so every agent step is auditable.

Coordinate model
----------------

Claude sees a downsampled screenshot whose width is ``downsample_width``
(default 1280). It returns click coordinates *in that downsampled frame*.
We must translate them back to macOS "points" (= CoreGraphics logical
pixels) before handing them to ``cliclick``. On this host the main display
reports pixel-width == point-width (no Retina scaling), so the scale factor
is purely ``physical_width / downsample_width``. On a Retina display you'd
additionally divide by the backing scale factor; ``_points_from_scaled``
handles that generically.

Requirement: CAT-06.
"""
from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# pyobjc is mac-only. Import guards so the module can be imported for
# coordinate-math unit tests on non-Darwin CI boxes.
try:  # pragma: no cover - platform guard
    import Quartz  # type: ignore[import-not-found]

    _HAS_QUARTZ = True
except Exception:  # pragma: no cover
    Quartz = None  # type: ignore[assignment]
    _HAS_QUARTZ = False

from PIL import Image


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #


@dataclass
class DisplayGeometry:
    """Geometry of the captured display.

    ``pixel_width`` × ``pixel_height`` is what Quartz gives us in the raw
    CGImage. ``point_width`` × ``point_height`` is what cliclick uses. On
    non-Retina screens they match.
    """

    pixel_width: int
    pixel_height: int
    point_width: int
    point_height: int

    @property
    def backing_scale(self) -> float:
        return self.pixel_width / max(self.point_width, 1)


@dataclass
class Trace:
    """Append-only step recorder under ``traces/<timestamp>/``."""

    root: Path
    step: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def new(cls, base_dir: Path) -> "Trace":
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        root = base_dir / stamp
        root.mkdir(parents=True, exist_ok=True)
        return cls(root=root)

    def record(
        self,
        kind: str,
        payload: dict[str, Any],
        png_bytes: bytes | None = None,
    ) -> Path:
        self.step += 1
        name = f"step_{self.step:03d}_{kind}"
        meta_path = self.root / f"{name}.json"
        meta = {"step": self.step, "kind": kind, "ts": time.time(), **payload}
        meta_path.write_text(json.dumps(meta, indent=2, default=str))
        png_path: Path | None = None
        if png_bytes is not None:
            png_path = self.root / f"{name}.png"
            png_path.write_bytes(png_bytes)
            meta["screenshot"] = str(png_path)
        self.events.append(meta)
        return meta_path


# --------------------------------------------------------------------------- #
# Display capture
# --------------------------------------------------------------------------- #


def get_display_geometry() -> DisplayGeometry:
    """Query the main display's pixel + point dimensions via Quartz."""
    if not _HAS_QUARTZ:  # pragma: no cover
        raise RuntimeError("Quartz unavailable; this tool requires macOS")
    main = Quartz.CGMainDisplayID()
    mode = Quartz.CGDisplayCopyDisplayMode(main)
    return DisplayGeometry(
        pixel_width=int(Quartz.CGDisplayModeGetPixelWidth(mode)),
        pixel_height=int(Quartz.CGDisplayModeGetPixelHeight(mode)),
        point_width=int(Quartz.CGDisplayModeGetWidth(mode)),
        point_height=int(Quartz.CGDisplayModeGetHeight(mode)),
    )


def _capture_cgimage():  # pragma: no cover - requires macOS + permissions
    """Grab the main display as a CGImage via Quartz."""
    main = Quartz.CGMainDisplayID()
    return Quartz.CGDisplayCreateImage(main)


def _cgimage_to_png_bytes(cg_image) -> bytes:  # pragma: no cover - macOS only
    width = Quartz.CGImageGetWidth(cg_image)
    height = Quartz.CGImageGetHeight(cg_image)
    bpr = Quartz.CGImageGetBytesPerRow(cg_image)
    provider = Quartz.CGImageGetDataProvider(cg_image)
    data = Quartz.CGDataProviderCopyData(provider)
    raw = bytes(data)
    # CGImage gives us BGRA; PIL wants RGBA. The frombuffer "BGRA" mode
    # decodes directly.
    img = Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", bpr, 1)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def take_screenshot(
    downsample_width: int = 1280,
    *,
    _raw_bytes: bytes | None = None,
) -> tuple[bytes, DisplayGeometry, tuple[int, int]]:
    """Capture the main display and return ``(png_bytes, geometry, (dw, dh))``.

    ``_raw_bytes`` is an injection hook used by unit tests.
    """
    geom = get_display_geometry()
    if _raw_bytes is None:  # pragma: no cover - live capture path
        cg = _capture_cgimage()
        if cg is None:
            raise RuntimeError(
                "CGDisplayCreateImage returned None — likely missing "
                "Screen Recording permission. Grant it in System "
                "Settings → Privacy & Security → Screen Recording "
                "for your terminal / Python binary, then restart the "
                "terminal."
            )
        raw_png = _cgimage_to_png_bytes(cg)
    else:
        raw_png = _raw_bytes
    img = Image.open(io.BytesIO(raw_png))
    if img.width > downsample_width:
        new_h = int(img.height * (downsample_width / img.width))
        img = img.resize((downsample_width, new_h), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue(), geom, (img.width, img.height)


# --------------------------------------------------------------------------- #
# Coordinate scaling
# --------------------------------------------------------------------------- #


def scaled_to_points(
    x: int,
    y: int,
    *,
    scaled_width: int,
    scaled_height: int,
    geometry: DisplayGeometry,
) -> tuple[int, int]:
    """Convert a Claude-reported ``(x, y)`` in the downsampled frame to
    screen *points* (cliclick coordinates).

    Points, not pixels — cliclick wants points. On a non-Retina display
    points == pixels. On Retina, ``pixel_width`` is e.g. 2× ``point_width``
    and we must divide out the backing scale factor.
    """
    if scaled_width <= 0 or scaled_height <= 0:
        raise ValueError("scaled dimensions must be positive")
    # Map from downsampled frame → pixel frame
    px_x = x * (geometry.pixel_width / scaled_width)
    px_y = y * (geometry.pixel_height / scaled_height)
    # Map from pixels → points
    pt_x = px_x / geometry.backing_scale
    pt_y = px_y / geometry.backing_scale
    return int(round(pt_x)), int(round(pt_y))


# --------------------------------------------------------------------------- #
# cliclick dispatch
# --------------------------------------------------------------------------- #


# Mapping from Claude "key" action key names to cliclick key codes.
# cliclick kp: uses short names; see `cliclick -h` / `man cliclick`.
_KEY_ALIAS = {
    "return": "return",
    "enter": "return",
    "tab": "tab",
    "escape": "esc",
    "esc": "esc",
    "space": "space",
    "up": "arrow-up",
    "down": "arrow-down",
    "left": "arrow-left",
    "right": "arrow-right",
    "page_up": "page-up",
    "page_down": "page-down",
    "home": "home",
    "end": "end",
    "delete": "delete",
    "backspace": "delete",
}


def _run_cliclick(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = shutil.which("cliclick")
    if cmd is None:
        raise RuntimeError("cliclick not on PATH; install with `brew install cliclick`")
    return subprocess.run(
        [cmd, *args], check=False, capture_output=True, text=True
    )


def _point_arg(x: int, y: int) -> str:
    return f"{x},{y}"


class Actuator:
    """Stateful dispatcher with a trace attached.

    ``dry_run`` governs the final export click ONLY via the agent's system
    prompt — this actuator executes every action physically so the model
    can still dismiss modals, open Sync Manager, and select the playlist.
    Policy-level "do not click Export" belongs in the prompt, not here.

    ``simulated`` is a separate flag used by unit tests that want to
    exercise the dispatch code paths without moving the mouse or touching
    the keyboard. Production callers should leave it at ``False``.
    """

    def __init__(
        self,
        trace: Trace,
        *,
        downsample_width: int = 1280,
        dry_run: bool = False,
        simulated: bool = False,
    ) -> None:
        self.trace = trace
        self.downsample_width = downsample_width
        self.dry_run = dry_run
        self.simulated = simulated
        self._last_geom: DisplayGeometry | None = None
        self._last_scaled: tuple[int, int] | None = None
        self._last_cursor: tuple[int, int] = (0, 0)

    # -- screenshot helper used by the agent loop --------------------- #
    def capture(self) -> dict[str, Any]:
        png, geom, scaled = take_screenshot(downsample_width=self.downsample_width)
        self._last_geom = geom
        self._last_scaled = scaled
        self.trace.record(
            "screenshot",
            {
                "physical_size": [geom.pixel_width, geom.pixel_height],
                "point_size": [geom.point_width, geom.point_height],
                "scaled_size": list(scaled),
                "bytes": len(png),
            },
            png_bytes=png,
        )
        return {
            "png_bytes": png,
            "geometry": geom,
            "scaled_size": scaled,
            "base64": base64.standard_b64encode(png).decode("ascii"),
        }

    # -- core dispatch ------------------------------------------------- #
    def execute_action(self, action: dict[str, Any]) -> dict[str, Any]:
        """Execute one Claude computer-use action.

        Supported action types (Claude spec subset):

        * ``screenshot``
        * ``mouse_move`` / ``cursor_position``
        * ``left_click`` / ``right_click`` / ``double_click``
        * ``left_click_drag``
        * ``type`` (literal text)
        * ``key`` (named key or ``"cmd+shift+s"`` combo)
        * ``wait``
        """
        a = action.get("action") or action.get("type")
        coord = action.get("coordinate")
        text = action.get("text")
        duration = action.get("duration", 0)

        payload: dict[str, Any] = {
            "action": a,
            "input": action,
            "dry_run": self.dry_run,
            "simulated": self.simulated,
        }

        if a == "screenshot":
            result = self.capture()
            payload["outcome"] = "captured"
            return {"ok": True, "screenshot": result["base64"], "meta": payload}

        if a in {"cursor_position"}:
            x, y = self._last_cursor
            payload["outcome"] = f"cursor={x},{y}"
            self.trace.record("action", payload)
            return {"ok": True, "x": x, "y": y, "meta": payload}

        # Actions that need a coordinate
        if a in {"mouse_move", "left_click", "right_click", "double_click"}:
            if coord is None:
                err = f"action {a!r} requires coordinate"
                payload["outcome"] = f"error: {err}"
                self.trace.record("action", payload)
                return {"ok": False, "error": err, "meta": payload}
            pt = self._to_points(coord)
            payload["point"] = list(pt)
            if self.simulated and a != "mouse_move":
                payload["outcome"] = "simulated skip"
                self.trace.record("action", payload)
                self._last_cursor = pt
                return {"ok": True, "skipped": True, "meta": payload}
            cli_cmd = {
                "mouse_move": ["m:"],
                "left_click": ["c:"],
                "right_click": ["rc:"],
                "double_click": ["dc:"],
            }[a]
            cli_cmd[0] = f"{cli_cmd[0]}{_point_arg(*pt)}"
            cp = _run_cliclick(cli_cmd)
            self._last_cursor = pt
            payload["cli"] = cli_cmd
            payload["outcome"] = "ok" if cp.returncode == 0 else f"rc={cp.returncode}"
            if cp.stderr:
                payload["stderr"] = cp.stderr.strip()
            self.trace.record("action", payload)
            return {"ok": cp.returncode == 0, "meta": payload}

        if a == "left_click_drag":
            start = action.get("start_coordinate") or action.get("start")
            end = coord or action.get("end_coordinate") or action.get("end")
            if start is None or end is None:
                err = "left_click_drag requires start_coordinate + coordinate"
                payload["outcome"] = f"error: {err}"
                self.trace.record("action", payload)
                return {"ok": False, "error": err, "meta": payload}
            s = self._to_points(start)
            e = self._to_points(end)
            payload["from"] = list(s)
            payload["to"] = list(e)
            if self.simulated:
                payload["outcome"] = "simulated skip"
                self.trace.record("action", payload)
                return {"ok": True, "skipped": True, "meta": payload}
            cp = _run_cliclick(
                [f"dd:{_point_arg(*s)}", f"du:{_point_arg(*e)}"]
            )
            payload["outcome"] = "ok" if cp.returncode == 0 else f"rc={cp.returncode}"
            self.trace.record("action", payload)
            return {"ok": cp.returncode == 0, "meta": payload}

        if a == "type":
            if not isinstance(text, str):
                err = "type action requires text"
                payload["outcome"] = f"error: {err}"
                self.trace.record("action", payload)
                return {"ok": False, "error": err, "meta": payload}
            payload["text"] = text
            if self.simulated:
                payload["outcome"] = "simulated skip"
                self.trace.record("action", payload)
                return {"ok": True, "skipped": True, "meta": payload}
            cp = _run_cliclick(["t:" + text])
            payload["outcome"] = "ok" if cp.returncode == 0 else f"rc={cp.returncode}"
            self.trace.record("action", payload)
            return {"ok": cp.returncode == 0, "meta": payload}

        if a == "key":
            if not isinstance(text, str):
                err = "key action requires text"
                payload["outcome"] = f"error: {err}"
                self.trace.record("action", payload)
                return {"ok": False, "error": err, "meta": payload}
            key_cmd = _translate_key_combo(text)
            payload["text"] = text
            payload["cli"] = key_cmd
            if self.simulated:
                payload["outcome"] = "simulated skip"
                self.trace.record("action", payload)
                return {"ok": True, "skipped": True, "meta": payload}
            cp = _run_cliclick(key_cmd)
            payload["outcome"] = "ok" if cp.returncode == 0 else f"rc={cp.returncode}"
            if cp.stderr:
                payload["stderr"] = cp.stderr.strip()
            self.trace.record("action", payload)
            return {"ok": cp.returncode == 0, "meta": payload}

        if a == "scroll":
            # Claude: {action, coordinate, scroll_direction, scroll_amount}.
            # cliclick has no native scroll. AppleScript's `scroll` via
            # System Events only works if the target app is frontmost and
            # has accessibility scroll events — which Rekordbox largely
            # does. We emit per-click synthetic scroll-wheel events by
            # shelling out to osascript. If this flakes, the user can
            # press PageDown / arrows from the prompt instead.
            direction = action.get("scroll_direction", "down")
            amount = int(action.get("scroll_amount", 3))
            if coord is not None:
                pt = self._to_points(coord)
                payload["point"] = list(pt)
                if not self.simulated:
                    _run_cliclick([f"m:{_point_arg(*pt)}"])
            payload["direction"] = direction
            payload["amount"] = amount
            if self.simulated:
                payload["outcome"] = "simulated skip"
                self.trace.record("action", payload)
                return {"ok": True, "skipped": True, "meta": payload}
            script = _build_scroll_applescript(direction, amount)
            cp = subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
            )
            payload["outcome"] = "ok" if cp.returncode == 0 else f"rc={cp.returncode}"
            if cp.stderr:
                payload["stderr"] = cp.stderr.strip()
            self.trace.record("action", payload)
            return {"ok": cp.returncode == 0, "meta": payload}

        if a == "wait":
            secs = float(duration or 1.0)
            payload["seconds"] = secs
            if not self.simulated:
                time.sleep(min(secs, 5.0))
            payload["outcome"] = "waited"
            self.trace.record("action", payload)
            return {"ok": True, "meta": payload}

        payload["outcome"] = f"unsupported action: {a!r}"
        self.trace.record("action", payload)
        return {"ok": False, "error": payload["outcome"], "meta": payload}

    def _to_points(self, coord: Any) -> tuple[int, int]:
        if self._last_geom is None or self._last_scaled is None:
            # First action before any screenshot — force one
            self.capture()
        assert self._last_geom is not None and self._last_scaled is not None
        x, y = coord[0], coord[1]
        return scaled_to_points(
            int(x),
            int(y),
            scaled_width=self._last_scaled[0],
            scaled_height=self._last_scaled[1],
            geometry=self._last_geom,
        )


def _build_scroll_applescript(direction: str, amount: int) -> str:
    """Return an AppleScript snippet that scrolls the frontmost app by
    firing arrow-key presses.

    Native wheel events via AppleScript are surprisingly unreliable with
    modern Metal-based apps. The safer proxy is ``key code`` via System
    Events — Page Down / Page Up / Down / Up — which most list views honour.
    ``amount`` is multiplied; scrolling "down 3" sends 3× arrow-down.
    """
    direction = direction.lower()
    # Key codes: up=126, down=125, left=123, right=124
    key_codes = {"up": 126, "down": 125, "left": 123, "right": 124}
    code = key_codes.get(direction, 125)
    # Clamp to avoid runaway key spam.
    n = max(1, min(int(amount), 10))
    return (
        f'tell application "System Events" to repeat {n} times\n'
        f'    key code {code}\n'
        f'    delay 0.05\n'
        f'end repeat'
    )


def _translate_key_combo(text: str) -> list[str]:
    """Translate a Claude ``key`` action string into cliclick args.

    Examples::

        "return"           → ["kp:return"]
        "cmd+shift+s"      → ["kd:cmd,shift", "kp:s", "ku:cmd,shift"]
        "cmd+c"            → ["kd:cmd", "kp:c", "ku:cmd"]
    """
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    modifier_set = {"cmd", "ctrl", "alt", "shift", "fn"}
    mods = [p for p in parts if p in modifier_set]
    keys = [p for p in parts if p not in modifier_set]
    if not keys:
        # Pure modifier keystroke — unlikely but handle gracefully
        return [f"kp:{mods[0]}"] if mods else ["w:0"]
    key = keys[0]
    key = _KEY_ALIAS.get(key, key)
    if not mods:
        return [f"kp:{key}"]
    mod_arg = ",".join(mods)
    return [f"kd:{mod_arg}", f"kp:{key}", f"ku:{mod_arg}"]
