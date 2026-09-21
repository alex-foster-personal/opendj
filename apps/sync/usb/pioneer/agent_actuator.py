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
(default 1920). It returns click coordinates *in that downsampled frame*.
We must translate them back to macOS "points" (= CoreGraphics logical
pixels) before handing them to ``cliclick``.

Two capture modes are supported:

* **window** (preferred) — we grab a specific Rekordbox window via
  ``CGWindowListCreateImage``. The downsampled frame has origin
  ``(0, 0)`` relative to the window's top-left; to get a global screen
  point we (a) rescale downsampled → window physical pixels, (b) divide
  by backing scale, (c) add the window's top-left origin in points.
* **display** (fallback) — we grab the main display. Downsampled coords
  rescale to the display's pixel frame, then map to points via backing
  scale.  Used when Rekordbox isn't on-screen.

Every ``capture()`` stashes a ``capture_meta`` dict (``capture_mode``,
``window_bounds``, ``downsample_*``, ``backing_scale``) that subsequent
``execute_action`` calls consult when translating clicks and rejecting
off-window clicks via the click-guard.

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
# Window-isolated capture  (Fix 1) + frontmost enforcement  (Fix 3)
# --------------------------------------------------------------------------- #


def find_app_window(
    app_name_substring: str,
) -> dict[str, Any] | None:  # pragma: no cover - requires live window list
    """Return the largest on-screen window whose owner name contains
    ``app_name_substring`` (case-insensitive), or None.

    Returned dict: ``window_id``, ``owner_name``, ``title``,
    ``bounds`` (``{x,y,w,h}`` in global points), ``layer``.
    """
    if not _HAS_QUARTZ:
        return None
    needle = app_name_substring.lower()
    opts = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    windows = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
    if not windows:
        return None
    candidates: list[dict[str, Any]] = []
    for w in windows:
        owner = (w.get("kCGWindowOwnerName") or "").lower()
        if needle not in owner:
            continue
        b = w.get("kCGWindowBounds") or {}
        width = float(b.get("Width", 0))
        height = float(b.get("Height", 0))
        if width < 100 or height < 100:
            continue
        candidates.append({
            "window_id": int(w.get("kCGWindowNumber", 0)),
            "owner_name": str(w.get("kCGWindowOwnerName") or ""),
            "title": str(w.get("kCGWindowName") or ""),
            "bounds": {
                "x": float(b.get("X", 0)),
                "y": float(b.get("Y", 0)),
                "w": width,
                "h": height,
            },
            "layer": int(w.get("kCGWindowLayer", 0)),
            "area": width * height,
        })
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c["layer"] != 0, -c["area"]))
    best = candidates[0]
    best.pop("area", None)
    return best


def _capture_window_cgimage(window_id: int):  # pragma: no cover - live only
    if not _HAS_QUARTZ:
        return None
    return Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        int(window_id),
        Quartz.kCGWindowImageBoundsIgnoreFraming
        | Quartz.kCGWindowImageNominalResolution,
    )


def _display_fallback_metadata(
    _png: bytes,
    geom: DisplayGeometry,
    scaled: tuple[int, int],
    *,
    prev_owner: str = "",
    prev_title: str = "",
) -> dict[str, Any]:
    return {
        "capture_mode": "display",
        "window_bounds": {
            "x": 0.0,
            "y": 0.0,
            "w": float(geom.point_width),
            "h": float(geom.point_height),
        },
        "window_id": None,
        "owner_name": prev_owner,
        "title": prev_title,
        "downsample_width": scaled[0],
        "downsample_height": scaled[1],
        "physical_width": geom.pixel_width,
        "physical_height": geom.pixel_height,
        "backing_scale": geom.backing_scale,
    }


def capture_window(
    app_name_substring: str = "rekordbox",
    downsample_width: int = 1920,
    *,
    _raw_bytes: bytes | None = None,
    _injected_window: dict[str, Any] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Capture a single app window and return ``(png_bytes, metadata)``.

    ``metadata`` has keys: ``capture_mode`` (``"window"`` or ``"display"``),
    ``window_bounds``, ``window_id``, ``owner_name``, ``title``,
    ``downsample_width``, ``downsample_height``, ``physical_width``,
    ``physical_height``, ``backing_scale``.

    Falls back to a full-display capture when no matching window is
    visible (capture_mode="display").
    """
    window = _injected_window if _injected_window is not None else find_app_window(app_name_substring)
    if window is None:
        png, geom, scaled = take_screenshot(
            downsample_width=downsample_width, _raw_bytes=_raw_bytes
        )
        return png, _display_fallback_metadata(png, geom, scaled)

    if _raw_bytes is None:  # pragma: no cover - live capture
        cg = _capture_window_cgimage(window["window_id"])
        if cg is None:
            png, geom, scaled = take_screenshot(downsample_width=downsample_width)
            return png, _display_fallback_metadata(
                png, geom, scaled,
                prev_owner=window.get("owner_name", ""),
                prev_title=window.get("title", ""),
            )
        raw_png = _cgimage_to_png_bytes(cg)
    else:
        raw_png = _raw_bytes

    img = Image.open(io.BytesIO(raw_png))
    physical_w, physical_h = img.width, img.height
    if img.width > downsample_width:
        new_h = int(img.height * (downsample_width / img.width))
        img = img.resize((downsample_width, new_h), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    w_points = float(window["bounds"]["w"]) or 1.0
    backing_scale = physical_w / w_points if w_points > 0 else 1.0
    return out.getvalue(), {
        "capture_mode": "window",
        "window_bounds": dict(window["bounds"]),
        "window_id": window["window_id"],
        "owner_name": window.get("owner_name", ""),
        "title": window.get("title", ""),
        "downsample_width": img.width,
        "downsample_height": img.height,
        "physical_width": physical_w,
        "physical_height": physical_h,
        "backing_scale": backing_scale,
    }


def window_scaled_to_global_points(
    x: int,
    y: int,
    metadata: dict[str, Any],
) -> tuple[int, int]:
    """Translate Claude-frame ``(x, y)`` to a global cliclick point.
    On window-mode this accounts for the window's on-screen origin.
    """
    dw = max(1, int(metadata.get("downsample_width", 1)))
    dh = max(1, int(metadata.get("downsample_height", 1)))
    physical_w = max(1, int(metadata.get("physical_width", dw)))
    physical_h = max(1, int(metadata.get("physical_height", dh)))
    px_x = x * (physical_w / dw)
    px_y = y * (physical_h / dh)
    backing = float(metadata.get("backing_scale") or 1.0) or 1.0
    pt_x = px_x / backing
    pt_y = px_y / backing
    bounds = metadata.get("window_bounds") or {"x": 0.0, "y": 0.0}
    gx = pt_x + float(bounds.get("x", 0.0))
    gy = pt_y + float(bounds.get("y", 0.0))
    return round(gx), round(gy)


def is_point_inside_window(
    global_x: int,
    global_y: int,
    metadata: dict[str, Any],
    *,
    margin: int = 2,
) -> bool:
    """Return True iff ``(global_x, global_y)`` falls inside the
    window described by ``metadata``.

    Display-mode always returns True (no isolated window to enforce).
    """
    if metadata.get("capture_mode") != "window":
        return True
    b = metadata.get("window_bounds") or {}
    x0 = float(b.get("x", 0.0)) - margin
    y0 = float(b.get("y", 0.0)) - margin
    x1 = x0 + float(b.get("w", 0.0)) + 2 * margin
    y1 = y0 + float(b.get("h", 0.0)) + 2 * margin
    return x0 <= global_x <= x1 and y0 <= global_y <= y1


def _frontmost_app_name() -> str | None:  # pragma: no cover - macOS only
    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        name = app.localizedName()
        return str(name) if name else None
    except Exception:
        return None


def _find_running_app(app_name: str) -> Any | None:  # pragma: no cover - macOS only
    """Return the running ``NSRunningApplication`` matching ``app_name``
    exactly (case-insensitive), or ``None``.

    Exact match, not substring: the deliberately-lingering
    ``rekordboxAgent`` helper (docs/QNA.md) would otherwise satisfy a
    substring check for "rekordbox" while the main app is closed, letting
    ``ensure_frontmost`` proceed straight to ``activate`` and cold-launch
    it. Returns ``None`` (not just a bool) so callers can reuse the exact
    reference for activation instead of a second, racy lookup-by-name.
    """
    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
    except ImportError:
        return None
    needle = app_name.lower()
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        name = app.localizedName()
        if name and str(name).lower() == needle:
            return app
    return None


def _app_is_running(app_name: str) -> bool:  # pragma: no cover - macOS only
    """True iff a running application matches ``app_name`` exactly.

    Matches NSWorkspace's running applications on localized name - the same
    source as ``_frontmost_app_name``, so "rekordbox" means the same thing
    to both probes. Falls back to ``pgrep -x`` when AppKit is unavailable.
    """
    try:
        import AppKit  # noqa: F401  type: ignore[import-not-found]
    except ImportError:
        # No pyobjc (non-macOS, or a bare venv): fall back to the process
        # table. Only the missing-import case is handled - anything else
        # is a real fault and must surface, not be masked.
        return _app_process_running(app_name)
    return _find_running_app(app_name) is not None


def ensure_frontmost(
    app_name: str = "rekordbox",
    *,
    max_attempts: int = 3,
    wait_ms: int = 500,
    allow_launch: bool = False,
    _frontmost_probe: Any = None,
    _activate: Any = None,
    _running_probe: Any = None,
) -> bool:
    """Ensure ``app_name`` is macOS frontmost. Returns True on success.

    AppleScript ``activate`` is launch-OR-focus, not focus: sent to an app
    that is not running, it LAUNCHES it. A "bring the window forward"
    helper must therefore refuse to act on a non-running app, or every
    caller silently becomes an "open Rekordbox" side effect - which is
    exactly what made Rekordbox cold-start on every test run (root cause
    in docs/QNA.md, Sun 31 Aug 2026).

    So: not running -> return False without touching osascript, unless the
    caller passes ``allow_launch=True`` to say it genuinely intends to
    start the app. Default is OFF, per no-hidden-side-effects.

    The not-running gate and the activation below share ONE
    ``NSRunningApplication`` reference (``running_app``) instead of a
    check-by-name followed by a separate activate-by-name: activating a
    held reference cannot launch a fresh instance even if the app quit in
    between, whereas AppleScript ``activate`` launches by BUNDLE NAME and
    would. A second by-name lookup would leave that exact window open
    again, just narrower.

    ``_frontmost_probe`` / ``_activate`` / ``_running_probe`` are test
    hooks. ``_running_probe`` (legacy) reports only a bool, so when it is
    supplied ``_do_activate`` defers to ``_activate``; real callers never
    supply ``_running_probe`` and go through the reference path below.
    """
    needle = app_name.lower()
    probe = _frontmost_probe or _frontmost_app_name

    running_app: Any = None
    if not allow_launch:
        if _running_probe is not None:
            if not _running_probe():
                return False
        else:
            running_app = _find_running_app(app_name)
            if running_app is None:
                return False

    def _do_activate() -> None:
        if _activate is not None:
            _activate()
            return
        if running_app is not None:
            # pragma: no cover - macOS only
            from AppKit import NSApplicationActivateIgnoringOtherApps

            running_app.activateWithOptions_(
                NSApplicationActivateIgnoringOtherApps
            )
            return
        # pragma: no cover - live only (allow_launch=True: launching by
        # name is the explicit intent here, no reference exists yet)
        subprocess.run(
            ["osascript", "-e", f'tell application "{app_name}" to activate'],
            check=False, capture_output=True, text=True,
        )

    for _ in range(max_attempts):
        current = probe()
        if current and current.lower() == needle:
            return True
        _do_activate()
        if wait_ms > 0:
            time.sleep(wait_ms / 1000.0)
    final = probe()
    return bool(final and final.lower() == needle)


def _app_process_running(app_name: str) -> bool:  # pragma: no cover - live
    """True iff a process named exactly ``app_name`` exists.

    Uses ``pgrep -x`` (exact process-name match), never ``pgrep -f``:
    the ``-f`` form matches this Python interpreter's own command line
    (self-match trap) and the lingering ``rekordboxAgent`` helper.
    """
    cp = subprocess.run(
        ["pgrep", "-x", app_name],
        check=False, capture_output=True, text=True,
    )
    return cp.returncode == 0


def quit_app(
    app_name: str = "rekordbox",
    *,
    max_wait_s: float = 20.0,
    poll_s: float = 0.5,
    _quit: Any = None,
    _running_probe: Any = None,
) -> bool:
    """Quit ``app_name`` via AppleScript and wait for its process to exit.

    Returns True once ``pgrep -x app_name`` finds nothing (also when the
    app was not running to begin with - idempotent). Returns False if the
    process is still alive after ``max_wait_s``: e.g. Rekordbox raised an
    "export in progress" prompt instead of quitting.

    AppleScript ``quit`` is a normal graceful shutdown (same as Cmd-Q),
    so Rekordbox runs its own teardown - it is not a kill and does not
    interrupt a database write mid-flight. The companion
    ``rekordboxAgent`` helper deliberately lingers after quit and is left
    alone. ``_quit`` / ``_running_probe`` are test hooks.
    """
    running = _running_probe or (lambda: _app_process_running(app_name))

    def _do_quit() -> None:
        if _quit is not None:
            _quit()
            return
        subprocess.run(  # pragma: no cover - live only
            ["osascript", "-e", f'tell application "{app_name}" to quit'],
            check=False, capture_output=True, text=True,
        )

    if not running():
        return True
    _do_quit()
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        if not running():
            return True
        if poll_s > 0:
            time.sleep(poll_s)
    return not running()


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
    return round(pt_x), round(pt_y)


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
        downsample_width: int = 1920,
        dry_run: bool = False,
        simulated: bool = False,
        window_app: str | None = "rekordbox",
        frontmost_app: str | None = "rekordbox",
        enforce_click_guard: bool = True,
    ) -> None:
        self.trace = trace
        self.downsample_width = downsample_width
        self.dry_run = dry_run
        self.simulated = simulated
        # When set, ``capture()`` isolates this app's window via
        # CGWindowListCreateImage (Fix 1). None = legacy full display.
        self.window_app = window_app
        # App to re-activate before click/type/key (Fix 3). None disables.
        self.frontmost_app = frontmost_app
        # Click-guard (Fix 6): reject mouse clicks whose translated
        # global point falls outside window-mode bounds.
        self.enforce_click_guard = enforce_click_guard
        self._last_geom: DisplayGeometry | None = None
        self._last_scaled: tuple[int, int] | None = None
        self._last_capture_meta: dict[str, Any] | None = None
        self._last_cursor: tuple[int, int] = (0, 0)

    # -- screenshot helper used by the agent loop --------------------- #
    def capture(self) -> dict[str, Any]:
        """Take a screenshot, record it in the trace, and return metadata.

        When ``self.window_app`` is set (the default), this prefers
        window-isolated capture (Fix 1). Returns a dict with
        ``png_bytes``, ``capture_meta``, ``scaled_size``, ``base64``,
        and (for display-mode compat) a ``geometry`` key when available.
        """
        if self.window_app is None:
            # Legacy full-display path.
            png, geom, scaled = take_screenshot(
                downsample_width=self.downsample_width
            )
            meta = _display_fallback_metadata(png, geom, scaled)
            self._last_geom = geom
            self._last_scaled = scaled
            self._last_capture_meta = meta
            self.trace.record(
                "screenshot",
                {
                    "physical_size": [geom.pixel_width, geom.pixel_height],
                    "point_size": [geom.point_width, geom.point_height],
                    "scaled_size": list(scaled),
                    "bytes": len(png),
                    "capture": meta,
                },
                png_bytes=png,
            )
            return {
                "png_bytes": png,
                "geometry": geom,
                "scaled_size": scaled,
                "capture_meta": meta,
                "base64": base64.standard_b64encode(png).decode("ascii"),
            }

        # Preferred: window-isolated capture.
        png, meta = capture_window(
            app_name_substring=self.window_app,
            downsample_width=self.downsample_width,
        )
        self._last_capture_meta = meta
        self._last_scaled = (
            int(meta["downsample_width"]),
            int(meta["downsample_height"]),
        )
        self.trace.record(
            "screenshot",
            {
                "bytes": len(png),
                "capture": meta,
            },
            png_bytes=png,
        )
        return {
            "png_bytes": png,
            "capture_meta": meta,
            "scaled_size": self._last_scaled,
            "base64": base64.standard_b64encode(png).decode("ascii"),
        }

    def _reject_if_focus_lost(
        self, action: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Re-activate ``frontmost_app`` before any input-producing action
        and reject the action if focus cannot be confirmed (Fix 3).

        Called from every dispatch branch that sends real input - click,
        ``left_click_drag``, ``type``, ``key``, and ``scroll`` - not only
        the click branch: Rekordbox may have quit or crashed after the
        screenshot, and dispatching cliclick/System Events anyway would
        send input to whatever app now sits at the stale coordinate or
        holds focus, so a failed check must reject the action rather than
        proceed. No-op for ``mouse_move`` (no focus needed), when
        ``simulated`` (unit tests), or when no ``frontmost_app`` is set.
        Returns an ``{"ok": False, ...}`` result to return early, or
        ``None`` to proceed.
        """
        if (
            action == "mouse_move"
            or self.simulated
            or self.frontmost_app is None
            or ensure_frontmost(self.frontmost_app, max_attempts=2)
        ):
            return None
        err = (
            f"could not confirm {self.frontmost_app!r} is frontmost; "
            "refusing to click at a possibly-stale coordinate"
        )
        payload["outcome"] = "rejected: focus enforcement failed"
        payload["guard_error"] = err
        self.trace.record("action", payload)
        return {"ok": False, "error": err, "meta": payload}

    # -- core dispatch ------------------------------------------------- #
    def _execute_pointer_action(
        self, action_type: str, coordinate: Any, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Dispatch one mouse move or click after its coordinate is validated."""
        if coordinate is None:
            err = f"action {action_type!r} requires coordinate"
            payload["outcome"] = f"error: {err}"
            self.trace.record("action", payload)
            return {"ok": False, "error": err, "meta": payload}
        point = self._to_points(coordinate)
        payload["point"] = list(point)
        click_guard = self._reject_outside_window_click(action_type, point, payload)
        if click_guard is not None:
            return click_guard
        focus_guard = self._reject_if_focus_lost(action_type, payload)
        if focus_guard is not None:
            return focus_guard
        if self.simulated and action_type != "mouse_move":
            payload["outcome"] = "simulated skip"
            self.trace.record("action", payload)
            self._last_cursor = point
            return {"ok": True, "skipped": True, "meta": payload}
        cli_cmd = {
            "mouse_move": ["m:"],
            "left_click": ["c:"],
            "right_click": ["rc:"],
            "double_click": ["dc:"],
        }[action_type]
        cli_cmd[0] = f"{cli_cmd[0]}{_point_arg(*point)}"
        cp = _run_cliclick(cli_cmd)
        self._last_cursor = point
        payload["cli"] = cli_cmd
        payload["outcome"] = "ok" if cp.returncode == 0 else f"rc={cp.returncode}"
        if cp.stderr:
            payload["stderr"] = cp.stderr.strip()
        self.trace.record("action", payload)
        return {"ok": cp.returncode == 0, "meta": payload}

    def _reject_outside_window_click(
        self,
        action_type: str,
        point: tuple[int, int],
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Reject a click outside the most recent isolated window capture."""
        is_click = action_type in {"left_click", "right_click", "double_click"}
        if (
            not self.enforce_click_guard
            or not is_click
            or self._last_capture_meta is None
            or is_point_inside_window(
                point[0], point[1], self._last_capture_meta
            )
        ):
            return None
        bounds = self._last_capture_meta.get("window_bounds") or {}
        err = (
            f"click at ({point[0]},{point[1]}) falls outside Rekordbox window bounds "
            f"x={bounds.get('x', 0):.0f},y={bounds.get('y', 0):.0f},"
            f"w={bounds.get('w', 0):.0f},h={bounds.get('h', 0):.0f}. "
            "Re-assess from the latest screenshot - the target UI element you "
            "clicked is NOT inside Rekordbox."
        )
        payload["outcome"] = "rejected: click outside window"
        payload["guard_error"] = err
        self.trace.record("action", payload)
        return {"ok": False, "error": err, "meta": payload}

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

        if a in {"mouse_move", "left_click", "right_click", "double_click"}:
            return self._execute_pointer_action(a, coord, payload)

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
            guard_result = self._reject_if_focus_lost(a, payload)
            if guard_result is not None:
                return guard_result
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
            guard_result = self._reject_if_focus_lost(a, payload)
            if guard_result is not None:
                return guard_result
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
            guard_result = self._reject_if_focus_lost(a, payload)
            if guard_result is not None:
                return guard_result
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
            payload["direction"] = direction
            payload["amount"] = amount
            guard_result = self._reject_if_focus_lost(a, payload)
            if guard_result is not None:
                return guard_result
            if coord is not None and not self.simulated:
                _run_cliclick([f"m:{_point_arg(*pt)}"])
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
        """Translate a Claude-frame ``(x, y)`` to a global cliclick point.

        Uses the latest capture metadata. On window-mode captures the
        result accounts for the window's on-screen origin (Fix 2).
        """
        if self._last_capture_meta is None:
            # First action before any screenshot — force one.
            self.capture()
        meta = self._last_capture_meta
        if meta is None:
            # Legacy path (unit tests that inject _last_geom + _last_scaled
            # without capture_meta). Use old math.
            assert self._last_geom is not None and self._last_scaled is not None
            x, y = coord[0], coord[1]
            return scaled_to_points(
                int(x),
                int(y),
                scaled_width=self._last_scaled[0],
                scaled_height=self._last_scaled[1],
                geometry=self._last_geom,
            )
        x, y = coord[0], coord[1]
        return window_scaled_to_global_points(int(x), int(y), meta)


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
