"""Classify filesystem paths that live in iCloud Drive sync zones.

Desktop & Documents sync makes ``~/Documents`` and
``~/Library/Mobile Documents/com~apple~CloudDocs`` often the same store
(same inode after realpath). Healing must land under configured MUSIC_ROOTS
outside this zone -- a Documents <-> CloudDocs string swap is not a heal.

TODO(machine-paths): iCloud-zone conjunctions and Music roots are host-local.
MUSIC_ROOTS defaults to $HOME/Music; other hosts must set MDT_MUSIC_ROOTS.
Cross-machine prefix relocation uses MDT_PATH_MAP / data/path-map.json --
heal-to-Music is not a substitute. Documents and CloudDocs are often the same
store. Apply RB rewrites only on the host that owns that Rekordbox library.
Future overrides (MDT_ICLOUD_ZONE_ROOTS / MDT_LOCAL_DOCUMENTS) belong in
explicit config, not silent guesses.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from apps.shared import platform_paths


def _home() -> Path:
    return platform_paths.HOME


def _mobile_documents() -> Path:
    return _home() / "Library" / "Mobile Documents"


def _cloud_docs() -> Path:
    return _mobile_documents() / "com~apple~CloudDocs"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _desktop_documents_sync_active() -> bool:
    """Fail-closed: CloudDocs directory present => Desktop & Documents sync."""
    try:
        return _cloud_docs().is_dir()
    except OSError:
        return False


def icloud_zone_reason(path: Path | str) -> str | None:
    """Return a short reason when ``path`` is in the iCloud zone; else None.

    Off-Darwin always None (this problem domain is macOS iCloud Drive).
    Missing paths classify lexically so broken FolderPath rows still match.
    """
    if sys.platform != "darwin":
        return None
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise ValueError(f"path must be absolute for icloud-zone classify: {path!r}")
    lexical = Path(os.path.normpath(raw))

    mobile = _mobile_documents()
    if _is_under(lexical, mobile) or lexical == mobile:
        return "mobile-documents"

    if _desktop_documents_sync_active():
        for root, reason in (
            (_home() / "Documents", "documents-desktop-sync"),
            (_home() / "Desktop", "desktop-desktop-sync"),
        ):
            if _is_under(lexical, root) or lexical == root:
                return reason

    # Existing path may realpath into CloudDocs while the lexical string
    # looked like a non-zone tree (rare symlink / alias cases).
    try:
        if raw.exists():
            real = Path(os.path.realpath(raw))
            if mobile.exists():
                mobile_real = Path(os.path.realpath(mobile))
                if _is_under(real, mobile_real) or real == mobile_real:
                    return "mobile-documents"
    except OSError:
        pass
    return None


def is_icloud_zone(path: Path | str) -> bool:
    """True when ``path`` is under Mobile Documents or synced Documents/Desktop."""
    return icloud_zone_reason(path) is not None


def assert_music_roots_outside_icloud_zone(
    roots: list[Path] | None = None,
) -> None:
    """Fail-fast when a configured MUSIC_ROOT resolves into the iCloud zone."""
    use_roots = roots if roots is not None else platform_paths.MUSIC_ROOTS
    for root in use_roots:
        reason = icloud_zone_reason(root)
        if reason is not None:
            raise RuntimeError(
                f"MUSIC_ROOT {root} is inside iCloud zone ({reason}); "
                "set MDT_MUSIC_ROOTS to a durable non-sync root (e.g. ~/Music). "
                "TODO(machine-paths): roots are host-local -- other machines differ."
            )
