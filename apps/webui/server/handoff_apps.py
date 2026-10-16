"""Hand a track's local file to rekordbox or djay Pro (HANDOFF-01..03).

The track detail page's "Open in Rekordbox" and "Open in djay" buttons call
this through ``routes/track_handoff.py``. Opening is the OS's own "open this
file with that app": Open DJ never writes either app's library, and it never
writes the audio file. What the target app does with the file (rekordbox adds
it to its collection, djay loads it) is that app's behavior, started by an
explicit click.

Discovery is a platform seam (AGENTS.md "Portable by default"):

- macOS: the newest ``rekordbox*.app`` / ``djay*.app`` under ``/Applications``
  or ``~/Applications`` (rekordbox installs as ``/Applications/rekordbox 7/
  rekordbox.app``), launched with ``/usr/bin/open -a <app> <file>``.
- Windows: the newest ``%ProgramFiles%\\{rekordbox,Pioneer}\\rekordbox */rekordbox.exe``,
  launched by its resolved path. djay Pro for Windows ships through the
  Microsoft Store with no stable file-open path, so it reports unavailable
  with that reason rather than guessing.
- Anything else: neither app exists, so both report unavailable.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.shared.platform_paths import is_unplayable_path

HandoffApp = Literal["rekordbox", "djay"]
HANDOFF_APPS: tuple[HandoffApp, ...] = ("rekordbox", "djay")
APP_LABELS: dict[HandoffApp, str] = {"rekordbox": "rekordbox", "djay": "djay Pro"}


class HandoffError(Exception):
    """Named failure for POST /api/v1/tracks/{stable_id}:open-in."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class HandoffTarget:
    """Where one hand-off app lives on this machine, or why it is unavailable."""

    app: HandoffApp
    label: str
    path: Path | None
    reason: str | None

    @property
    def available(self) -> bool:
        return self.path is not None


def _version_key(path: Path) -> tuple[int, ...]:
    """Sort key that ranks ``rekordbox 7`` above ``rekordbox 6.8.5``."""
    digits = "".join(ch if ch.isdigit() else " " for ch in str(path))
    return tuple(int(part) for part in digits.split())


def _newest(candidates: list[Path]) -> Path | None:
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None
    return max(existing, key=_version_key)


def _mac_app_dirs() -> list[Path]:
    return [Path("/Applications"), Path.home() / "Applications"]


def _find_mac(app: HandoffApp, app_dirs: list[Path]) -> Path | None:
    candidates: list[Path] = []
    for root in app_dirs:
        if not root.is_dir():
            continue
        if app == "rekordbox":
            candidates += list(root.glob("rekordbox*/rekordbox.app"))
            candidates += list(root.glob("rekordbox*.app"))
        else:
            candidates += list(root.glob("djay*.app"))
    return _newest(candidates)


def _find_windows_rekordbox(program_files: str | None) -> Path | None:
    if not program_files:
        return None
    # Older installers use ``rekordbox\``; Pioneer's documented layout is
    # ``Pioneer\rekordbox <version>\``. Take the newest across both.
    base = Path(program_files)
    candidates = [
        *(base / "rekordbox").glob("rekordbox */rekordbox.exe"),
        *(base / "Pioneer").glob("rekordbox */rekordbox.exe"),
    ]
    return _newest(candidates)


def resolve_target(
    app: HandoffApp,
    *,
    system: str | None = None,
    mac_app_dirs: list[Path] | None = None,
    program_files: str | None = None,
) -> HandoffTarget:
    """Locate ``app`` on this machine; ``reason`` names why when it is absent.

    ``program_files`` defaults to ``%ProgramFiles%`` on Windows.
    """
    label = APP_LABELS[app]
    system = system or platform.system()
    path: Path | None = None
    reason: str | None = None
    if system == "Darwin":
        path = _find_mac(app, mac_app_dirs if mac_app_dirs is not None else _mac_app_dirs())
        if path is None:
            reason = f"{label} is not installed in /Applications on this Mac"
    elif system == "Windows":
        if app == "rekordbox":
            path = _find_windows_rekordbox(program_files or os.environ.get("PROGRAMFILES"))
            if path is None:
                reason = f"{label} is not installed under Program Files on this PC"
        else:
            reason = f"{label} for Windows has no file-open path Open DJ can call"
    else:
        reason = f"{label} does not run on {system or 'this platform'}"
    return HandoffTarget(app=app, label=label, path=path, reason=reason)


# macOS's LaunchServices opener. A fixed system path, never a PATH lookup:
# a restricted PATH would hide it and a shadowing ``open`` would run instead.
MAC_OPENER = Path("/usr/bin/open")


def _launch_argv(target: HandoffTarget, file_path: Path, system: str, mac_opener: Path) -> list[str]:
    assert target.path is not None
    if system == "Darwin":
        return [str(mac_opener), "-a", str(target.path), str(file_path)]
    return [str(target.path), str(file_path)]


def _launch(target: HandoffTarget, argv: list[str], system: str) -> None:
    """Run ``argv``; any launch failure becomes a ``HandoffError``."""
    try:
        if system == "Darwin":
            # `open` returns as soon as LaunchServices has the request.
            subprocess.run(argv, check=True, timeout=10)
        else:
            # A Windows exe is the app itself: start it, never wait on it.
            subprocess.Popen(argv)  # argv is a resolved exe + file path
    except FileNotFoundError as exc:
        raise HandoffError("handoff_unavailable", f"launch command unavailable: {argv}") from exc
    except subprocess.CalledProcessError as exc:
        raise HandoffError("handoff_failed", f"{target.label} did not open the file: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HandoffError("handoff_failed", f"{target.label} did not answer within 10 s") from exc


def open_in_app(
    app: HandoffApp,
    file_path: str | None,
    *,
    system: str | None = None,
    mac_app_dirs: list[Path] | None = None,
    mac_opener: Path = MAC_OPENER,
) -> HandoffTarget:
    """Open ``file_path`` in ``app`` or raise ``HandoffError`` naming why not.

    ``mac_opener`` is the resolved opener program on macOS; callers other
    than tests leave it at :data:`MAC_OPENER`.
    """
    if not file_path or is_unplayable_path(file_path):
        raise HandoffError("not_a_local_file", "track has no local file path")
    path = Path(file_path)
    if not path.exists():
        raise HandoffError("file_missing", f"file not found: {file_path}")
    system = system or platform.system()
    target = resolve_target(app, system=system, mac_app_dirs=mac_app_dirs)
    if not target.available:
        raise HandoffError("app_not_installed", target.reason or f"{target.label} unavailable")
    if system == "Darwin" and not mac_opener.is_file():
        raise HandoffError("handoff_unavailable", f"macOS opener not found at {mac_opener}")
    _launch(target, _launch_argv(target, path, system, mac_opener), system)
    return target
