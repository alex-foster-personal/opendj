"""Single home for every OS-branching path decision in the codebase.

``apps/shared/paths.py`` re-exports the platform-dependent constants defined
here so existing ``from apps.shared.paths import ...`` call sites keep
working unchanged. New code that needs OS-aware resolution (the Windows
portability path map, the share-root rule) should import from this module
directly.

Fail-fast contract: nothing here silently guesses a path. An unresolvable
foreign-absolute path (e.g. a Mac ``/Users/...`` FolderPath read on Windows
with no matching :class:`PathMap` entry) comes back as an explicit
``MappedPath(resolved=None, mapped=False, reason="unmapped:<platform>")`` --
never a fabricated ``Path`` that happens to not exist.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ----- Platform identity --------------------------------------------------
PLATFORM: str = sys.platform
IS_DARWIN: bool = PLATFORM == "darwin"
IS_WINDOWS: bool = PLATFORM == "win32"

HOME: Path = Path.home()

# ----- Project root / data dir (platform-independent, but needed here for
# ----- load_path_map's default source) ------------------------------------
# This file lives at ``<project>/apps/shared/platform_paths.py`` ->
# parents[2] is the project root.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DATA_DIR: Path = Path(os.environ.get("MDT_DATA_DIR", PROJECT_ROOT / "data"))


def rekordbox_app_dir() -> Path:
    """Return the platform-specific rekordbox application directory.

    darwin: ``~/Library/Pioneer/rekordbox``.
    win32: ``%APPDATA%/Pioneer/rekordbox``; raises ``RuntimeError`` if
    ``APPDATA`` is unset (fail fast -- never guess a Windows profile path).
    else (linux/CI): ``~/.Pioneer/rekordbox`` -- a documented placeholder
    used only for path composition, never required to exist.
    """
    if IS_DARWIN:
        return HOME / "Library" / "Pioneer" / "rekordbox"
    elif IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError(
                "APPDATA environment variable is not set; cannot locate the "
                "Windows rekordbox app dir (%APPDATA%/Pioneer/rekordbox)."
            )
        return Path(appdata) / "Pioneer" / "rekordbox"
    else:
        return HOME / ".Pioneer" / "rekordbox"


REKORDBOX_APP_DIR: Path = rekordbox_app_dir()
REKORDBOX_LIVE_DB: Path = REKORDBOX_APP_DIR / "master.db"
SHARE_ROOT: Path = REKORDBOX_APP_DIR / "share"

# ----- djay Pro -------------------------------------------------------------
# djay Pro is macOS-only; off-darwin this Path is still composed (consumers
# already guard with .exists()) but documented as unavailable.
DJAY_LIVE_DB: Path = (
    HOME / "Music" / "djay" / "djay Media Library.djayMediaLibrary" / "MediaLibrary.db"
)

# ----- Filesystem music library --------------------------------------------
def _parse_music_roots(configured_roots: str) -> list[Path]:
    """Parse a configured, path-separator-delimited music-root list."""
    roots = [
        Path(entry.strip()).expanduser()
        for entry in configured_roots.split(os.pathsep)
        if entry.strip()
    ]
    if not roots:
        raise ValueError("MDT_MUSIC_ROOTS is configured but contains no usable paths.")
    return roots


MUSIC_ROOTS: list[Path] = (
    _parse_music_roots(os.environ["MDT_MUSIC_ROOTS"])
    if "MDT_MUSIC_ROOTS" in os.environ
    else [HOME / "Music"]
)

STREAMING_PREFIXES: tuple[str, ...] = ("tidal:", "soundcloud:", "spotify:")


# ----- Path map (explicit Mac -> Windows library relocation) ---------------
@dataclass(frozen=True)
class PathMap:
    """Ordered set of ``(from_prefix, to_prefix)`` rewrite rules.

    ``entries`` is sorted longest-``from`` first by :func:`load_path_map` so
    nested prefixes resolve to their most specific match.
    """

    entries: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MappedPath:
    """Result of :func:`resolve_library_path`.

    ``resolved is None`` IS the explicit "could not resolve this path on
    this machine" state -- callers must handle it, never assume a Path.
    """

    original: str
    resolved: Optional[Path]
    mapped: bool
    reason: str


_EMPTY_PATH_MAP: PathMap = PathMap(entries=())


def _normalise_map_prefix(path: str) -> str:
    """Trim a non-root map separator without turning a drive root relative."""
    if path == "/" or (len(path) == 3 and path[1] == ":" and path[2] in "/\\\\"):
        return path
    return path.rstrip("/\\\\")


def load_path_map(data_dir: Optional[Path] = None) -> PathMap:
    """Load the active :class:`PathMap`.

    Source order: env ``MDT_PATH_MAP`` (path to a JSON file) -> else
    ``data_dir / "path-map.json"`` when supplied -> else
    ``DATA_DIR / "path-map.json"`` -> else an empty map.

    JSON shape: ``{"entries": [{"from": "...", "to": "..."}]}``. Entries are
    sorted longest-``from`` first so nested prefixes win. Raises on
    malformed JSON or a non-list ``entries`` -- fail fast, never swallow a
    broken config.
    """
    env_path = os.environ.get("MDT_PATH_MAP")
    if env_path:
        map_path = Path(env_path)
    else:
        map_path = (data_dir or DATA_DIR) / "path-map.json"

    if not map_path.is_file():
        return _EMPTY_PATH_MAP

    try:
        raw = json.loads(map_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON in path map {map_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"path map {map_path} must be a JSON object, got {type(raw).__name__}")

    entries_raw = raw.get("entries", [])
    if not isinstance(entries_raw, list):
        raise ValueError(
            f"path map {map_path} 'entries' must be a list, got {type(entries_raw).__name__}"
        )

    entries: list[tuple[str, str]] = []
    for item in entries_raw:
        if not isinstance(item, dict) or "from" not in item or "to" not in item:
            raise ValueError(
                f"path map {map_path} entry must be an object with 'from' and 'to' keys, got {item!r}"
            )
        from_prefix = item["from"]
        to_prefix = item["to"]
        if not isinstance(from_prefix, str) or not isinstance(to_prefix, str):
            raise ValueError(f"path map {map_path} entries must contain string paths")
        if not from_prefix or not to_prefix:
            raise ValueError(f"path map {map_path} entries must not contain empty paths")
        if not _is_any_absolute(from_prefix) or not _is_any_absolute(to_prefix):
            raise ValueError(f"path map {map_path} entries must use absolute paths")
        if _has_parent_reference(from_prefix) or _has_parent_reference(to_prefix):
            raise ValueError(f"path map {map_path} entries must not contain '..' segments")
        normalised_from = _normalise_map_prefix(from_prefix)
        normalised_to = _normalise_map_prefix(to_prefix)
        if not _is_any_absolute(normalised_from) or not _is_any_absolute(normalised_to):
            raise ValueError(f"path map {map_path} entries must use absolute paths")
        entries.append((normalised_from, normalised_to))

    entries.sort(key=lambda pair: len(pair[0]), reverse=True)
    return PathMap(entries=tuple(entries))


def _is_foreign_absolute(path: str) -> bool:
    """True if ``path`` is an absolute path native to a DIFFERENT OS.

    On win32: a POSIX-rooted path (leading ``/``, not a drive letter or
    UNC share) is foreign.
    On non-win32: a Windows drive-letter path (``C:\\...`` / ``C:/...``) or
    a UNC path (``\\\\server\\share``) is foreign.

    Explicit and small on purpose -- no ``os.path`` heuristics that guess.
    """
    if IS_WINDOWS:
        return path.startswith("/")
    else:
        if path.startswith("\\\\"):
            return True
        if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
            return True
        return False


def _is_any_absolute(path: str) -> bool:
    """Return whether ``path`` is absolute in either supported syntax."""
    return (
        path.startswith("/")
        or path.startswith("\\\\")
        or (len(path) >= 2 and path[1] == ":" and path[0].isalpha())
    )


def _has_parent_reference(path: str) -> bool:
    """Reject lexical traversal before it reaches a filesystem operation."""
    return ".." in path.replace("\\\\", "/").split("/")


def _path_map_suffix(folder_path: str, from_prefix: str) -> Optional[str]:
    """Return a boundary-safe mapped suffix, or ``None`` when no match.

    Plain ``startswith`` maps ``/Users/dj/Music-old`` through a
    ``/Users/dj/Music`` rule.  That is not a prefix containment relation.
    """
    if folder_path == from_prefix:
        return ""
    if not folder_path.startswith(from_prefix):
        return None
    suffix = folder_path[len(from_prefix):]
    if not suffix.startswith(("/", "\\\\")) or _has_parent_reference(suffix):
        return None
    return suffix


def _is_native_absolute(path: str) -> bool:
    """True if ``path`` is absolute in the format THIS OS understands."""
    if IS_WINDOWS:
        if path.startswith("\\\\"):
            return True
        if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
            return True
        return False
    else:
        return path.startswith("/")


def resolve_library_path(
    folder_path: str, *, path_map: Optional[PathMap] = None
) -> MappedPath:
    """Resolve a state.db/rekordbox ``FolderPath`` to a filesystem path.

    The ONE resolver both the webui backend and the vocals CLI call. Order:

    1. Empty / streaming (tidal:/soundcloud:/spotify:) -> unmapped, explicit
       reason "streaming" (callers already special-case streaming upstream,
       but this stays defensive).
    2. ``/PIONEER/...`` share-relative -> ``SHARE_ROOT / path.lstrip("/")``,
       reason "share".
    3. Absolute path native to THIS OS -> ``Path(folder_path)`` verbatim,
       reason "native".
    4. Foreign-absolute (e.g. a Mac path read on Windows) -> try each
       :class:`PathMap` entry longest-prefix-first; a hit rewrites the
       prefix (reason "path-map"); no hit -> ``resolved=None, mapped=False,
       reason="unmapped:<platform>"``. This is the load-bearing fail-fast:
       never fabricate a path that might not exist.
    """
    if path_map is None:
        path_map = load_path_map()

    if not folder_path or folder_path.startswith(STREAMING_PREFIXES):
        return MappedPath(original=folder_path, resolved=None, mapped=False, reason="streaming")

    if folder_path.startswith("/PIONEER/"):
        if _has_parent_reference(folder_path):
            return MappedPath(
                original=folder_path,
                resolved=None,
                mapped=False,
                reason="unsafe:share-path",
            )
        resolved = SHARE_ROOT / folder_path.lstrip("/")
        return MappedPath(original=folder_path, resolved=resolved, mapped=True, reason="share")

    if _is_native_absolute(folder_path):
        return MappedPath(
            original=folder_path, resolved=Path(folder_path), mapped=True, reason="native"
        )

    if _is_foreign_absolute(folder_path):
        for from_prefix, to_prefix in path_map.entries:
            suffix = _path_map_suffix(folder_path, from_prefix)
            if suffix is not None:
                rewritten = to_prefix + suffix
                return MappedPath(
                    original=folder_path,
                    resolved=Path(rewritten),
                    mapped=True,
                    reason="path-map",
                )
        return MappedPath(
            original=folder_path, resolved=None, mapped=False, reason=f"unmapped:{PLATFORM}"
        )

    # Neither native nor recognisably foreign (e.g. a bare relative path).
    return MappedPath(
        original=folder_path, resolved=None, mapped=False, reason=f"unmapped:{PLATFORM}"
    )
