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

from apps.shared import fs_residency
from apps.shared.library_mode import crate_root, is_mac_users_path

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


def compute_share_root() -> Path:
    """Pioneer share root for this process.

    Remote mode serves ANLZ from ``$MDT_CRATE_ROOT/pioneer-share`` so
    ``/PIONEER/...`` AnalysisDataPath rows land in the crate, not
    ``~/.Pioneer/rekordbox/share``. Local mode keeps the platform app dir.
    Explicit ``MDT_LIBRARY_MODE=remote`` only -- never inferred from hostname.
    """
    raw_mode = os.environ.get("MDT_LIBRARY_MODE", "").strip().lower()
    if raw_mode == "remote":
        return crate_root() / "pioneer-share"
    return rekordbox_app_dir() / "share"


def refresh_share_root() -> Path:
    """Recompute :data:`SHARE_ROOT` after library-mode env is applied."""
    global SHARE_ROOT
    SHARE_ROOT = compute_share_root()
    return SHARE_ROOT


SHARE_ROOT: Path = compute_share_root()

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

# ----- Streaming / non-local path classification --------------------------
#
# ONE prefix set and ONE URI test for the whole repo (T3b map D1). Before
# this, four copies disagreed: rb_vendor and crate_sync knew "soundcloud:"
# but not "http(s)://", rekordbox_db knew "http(s)://" but not
# "soundcloud:". Neither omission was deliberate -- both sets were just
# incomplete -- so the union is strictly more correct for every caller.
#
# Two predicates, because callers genuinely ask two different questions and
# collapsing them corrupts data in both directions (see the T3b commit body):
#   is_streaming_uri  -- "is this a streaming-service URI?"   ""/None -> False
#   is_unplayable_path -- "is there no local file here?"      ""/None -> True
STREAMING_PREFIXES: tuple[str, ...] = (
    "tidal:",
    "soundcloud:",
    "spotify:",
    "http://",
    "https://",
)


def is_streaming_uri(path: str | None) -> bool:
    """True iff ``path`` is a non-empty streaming-service URI.

    An empty/None path is NOT a streaming URI -- it is an absent path. Wire
    fields that report "this track streams" (the browser read model's
    ``is_streaming``) must use this, or a track with no FolderPath at all
    gets rendered as a Spotify row.
    """
    return bool(path) and str(path).startswith(STREAMING_PREFIXES)


def is_unplayable_path(path: str | None) -> bool:
    """True iff ``path`` names no local file: empty/None, or a streaming URI.

    This is the "skip it, there is nothing on disk" predicate. An empty path
    must answer True here, or callers build ``Path("")`` -- which is
    ``Path(".")``, an existing directory -- and classify a pathless track as
    a present local file.
    """
    return not path or is_streaming_uri(path)


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


def _rewrite_with_path_map(folder_path: str, path_map: PathMap) -> Optional[str]:
    """Rewrite ``folder_path`` through the first matching prefix, or None."""
    for from_prefix, to_prefix in path_map.entries:
        suffix = _path_map_suffix(folder_path, from_prefix)
        if suffix is not None:
            return to_prefix + suffix
    return None


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
    3. Remote mode + Mac ``/Users/...`` prefix: skip native (a leftover
       ``/Users/dev`` tree on Linux must not win). Path-map only, or
       ``resolved=None, reason="unmapped:remote"``.
    4. Absolute path native to THIS OS: if the file is materialised here,
       use it (reason "native"). If it is missing, apply :class:`PathMap`
       even when the stored path looks native -- a Mac ``/Users/...`` path
       is POSIX-native on Linux, so the old "foreign only" rule never
       remapped the crate onto agentbox.
    5. Foreign-absolute (e.g. a Mac path read on Windows) -> path-map or
       ``resolved=None, reason="unmapped:<platform>"``. Never fabricate a
       path that might not exist.
    """
    if path_map is None:
        path_map = load_path_map()

    if is_unplayable_path(folder_path):
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

    rewritten = _rewrite_with_path_map(folder_path, path_map)
    remote_mode = os.environ.get("MDT_LIBRARY_MODE", "").strip().lower() == "remote"

    if remote_mode and is_mac_users_path(folder_path):
        if rewritten is not None:
            return MappedPath(
                original=folder_path,
                resolved=Path(rewritten),
                mapped=True,
                reason="path-map",
            )
        return MappedPath(
            original=folder_path, resolved=None, mapped=False, reason="unmapped:remote"
        )

    if _is_native_absolute(folder_path):
        native_path = Path(folder_path)
        if fs_residency.is_materialised(native_path):
            return MappedPath(
                original=folder_path, resolved=native_path, mapped=True, reason="native"
            )
        if rewritten is not None:
            return MappedPath(
                original=folder_path,
                resolved=Path(rewritten),
                mapped=True,
                reason="path-map",
            )
        return MappedPath(
            original=folder_path, resolved=native_path, mapped=True, reason="native"
        )

    if _is_foreign_absolute(folder_path):
        if rewritten is not None:
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


def _contained_asset_path(mapped: MappedPath, candidate: Path) -> MappedPath:
    """Resolve an asset candidate and reject a share-root symlink escape."""
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return MappedPath(
            original=mapped.original,
            resolved=None,
            mapped=False,
            reason="unsafe:resolution-error",
        )
    if mapped.reason == "share":
        root = SHARE_ROOT.resolve()
        if not resolved.is_relative_to(root):
            return MappedPath(
                original=mapped.original,
                resolved=None,
                mapped=False,
                reason="unsafe:share-symlink",
            )
    return MappedPath(
        original=mapped.original,
        resolved=resolved,
        mapped=mapped.mapped,
        reason=mapped.reason,
    )


def resolve_asset_path(
    asset_path: str, *, path_map: Optional[PathMap] = None
) -> MappedPath:
    """Map one vendor asset path and enforce symlink-aware containment.

    An explicit ``path_map`` keeps callers bound to their selected data root
    instead of silently consulting the process-default configuration.
    """
    mapped = resolve_library_path(asset_path, path_map=path_map)
    if mapped.resolved is None:
        return mapped
    return _contained_asset_path(mapped, mapped.resolved)


def resolve_asset_sibling(mapped: MappedPath, candidate: Path) -> MappedPath:
    """Contain a derived sibling of an already-mapped vendor asset path."""
    return _contained_asset_path(mapped, candidate)
