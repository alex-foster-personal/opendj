"""macOS desktop side: open Finder + rekordbox side by side, read idleness, quit.

No TCC grant is needed for the reads: ``ioreg`` gives system-wide input idle
time and ``lsappinfo`` gives the frontmost app. Positioning the rekordbox
window goes through System Events, which needs Accessibility for the calling
process; when that is refused the helper says so loudly and carries on, because
window placement is cosmetic and never gates the import.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from apps.rekordbox_ingestion.config import CFG


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=30)


def _osascript(script: str) -> subprocess.CompletedProcess[str]:
    return _run(["osascript", "-e", script], check=False)


# ----- idle / frontmost ------------------------------------------------------------


def hid_idle_seconds() -> float:
    """Seconds since ANY keyboard/mouse input, system-wide (IOHIDSystem HIDIdleTime)."""
    out = _run(["ioreg", "-c", "IOHIDSystem", "-d", "4"]).stdout
    match = re.search(r'"HIDIdleTime" = (\d+)', out)
    if match is None:
        raise RuntimeError("ioreg returned no HIDIdleTime; cannot judge idleness")
    return int(match.group(1)) / 1e9


def frontmost_app_name() -> str:
    asn = _run(["lsappinfo", "front"]).stdout.strip()
    out = _run(["lsappinfo", "info", asn]).stdout
    # First line is `"<display name>" ASN:0x...: (in front)`.
    match = re.match(r'\s*"([^"]+)" ASN:', out)
    if match is None:
        raise RuntimeError(f"lsappinfo gave no name for front app {asn!r}: {out!r}")
    return match.group(1)


@dataclass
class TouchTracker:
    """Remembers the last moment the maintainer was using rekordbox.

    A poll counts as a touch when rekordbox is frontmost AND there was input
    during the last poll interval. Quitting is allowed only after
    ``idle_minutes`` with no touch AND only once the tracker has itself been
    watching for that long, so a fresh tracker never authorizes a quit.
    """

    idle_minutes: float
    started_at: float
    last_touch_at: float | None = None

    def observe(self, now: float, frontmost: str, hid_idle: float, poll_seconds: float) -> None:
        if frontmost.lower() == "rekordbox" and hid_idle < poll_seconds:
            self.last_touch_at = now - hid_idle

    def may_quit(self, now: float, frontmost: str, hid_idle: float) -> tuple[bool, str]:
        window = self.idle_minutes * 60
        if now - self.started_at < window:
            return False, f"watched only {int(now - self.started_at)}s of the {int(window)}s window"
        if frontmost.lower() == "rekordbox" and hid_idle < window:
            return False, f"rekordbox is frontmost and input was {int(hid_idle)}s ago"
        if self.last_touch_at is not None and now - self.last_touch_at < window:
            return False, f"rekordbox was last used {int(now - self.last_touch_at)}s ago"
        return True, f"no rekordbox use for at least {int(window)}s"


# ----- open / arrange / quit -------------------------------------------------------


def open_side_by_side(batch_dir: Path, *, dry_run: bool) -> list[str]:
    """Open Finder at the batch (left half) and rekordbox (right half). Returns warnings."""
    steps = [
        f"open {batch_dir}  (Finder window on the batch)",
        f"open -b {CFG.REKORDBOX_BUNDLE_ID}  (launch / front rekordbox)",
        "place Finder on the left half and rekordbox on the right half",
    ]
    if dry_run:
        return [f"[dry-run] would: {s}" for s in steps]
    if not batch_dir.is_dir():
        raise FileNotFoundError(f"batch dir does not exist: {batch_dir}")
    _run(["open", str(batch_dir)])
    _run(["open", "-b", CFG.REKORDBOX_BUNDLE_ID])
    bounds = _osascript('tell application "Finder" to get bounds of window of desktop')
    if bounds.returncode != 0:
        raise RuntimeError(f"cannot read screen bounds: {bounds.stderr.strip()}")
    _, _, width, height = (int(v) for v in bounds.stdout.strip().split(", "))
    half = width // 2
    warnings: list[str] = []
    finder = _osascript(f'tell application "Finder" to set bounds of front window to {{0, 25, {half}, {height}}}')
    if finder.returncode != 0:
        warnings.append(f"[WARN] could not place Finder: {finder.stderr.strip()}")
    placed = no_access = False
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not placed:
        result = _osascript(
            'tell application "System Events" to tell process "rekordbox" to '
            f"set {{position, size}} of front window to {{{{{half}, 25}}, {{{width - half}, {height - 25}}}}}"
        )
        placed = result.returncode == 0
        if not placed and "assistive access" in result.stderr:
            warnings.append(
                "[WARN] could not place rekordbox: this process lacks Accessibility "
                "(System Settings > Privacy & Security > Accessibility). Drag it to the right half."
            )
            no_access = True
            break
        if not placed:
            time.sleep(3)
    if not placed and not no_access:
        warnings.append("[WARN] rekordbox window did not appear within 90s to be placed")
    return warnings


def quit_rekordbox_gracefully(*, wait_seconds: int = CFG.QUIT_WAIT_SECONDS) -> bool:
    """AppleScript quit (never kill). True once every rekordbox process is gone."""
    from apps.rekordbox_ingestion.playlists import running_rekordbox_processes

    try:
        _osascript(f'tell application id "{CFG.REKORDBOX_BUNDLE_ID}" to quit')
    except subprocess.TimeoutExpired:
        return False  # a modal dialog is holding the quit event; rekordbox is still up
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not [p for p in running_rekordbox_processes() if p.startswith("rekordbox(")]:
            return True
        time.sleep(3)
    return False
