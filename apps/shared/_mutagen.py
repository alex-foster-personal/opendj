"""Opt-in import gate for the GPL ``mutagen`` extra.

The default install does not depend on mutagen. Callers that write tags
through the optional extra import this module and call :func:`require`.
Reads, artwork and the packaged payload do not.
"""
from __future__ import annotations

try:  # pragma: no cover - exercised when the extra is installed
    import mutagen as _mutagen
    HAS_MUTAGEN: bool = True
except ImportError:  # pragma: no cover - the default install
    _mutagen = None  # type: ignore[assignment]
    HAS_MUTAGEN = False

_INSTALL_HINT = (
    "tag writing requires the optional 'mutagen' dependency. "
    "Install with: pip install 'music-dj-tools[tags]' "
    "(mutagen is GPL-2.0-or-later and therefore shipped as an opt-in extra)."
)


def require() -> None:
    """Raise ImportError when the optional mutagen extra is not installed."""
    if not HAS_MUTAGEN:
        raise ImportError(_INSTALL_HINT)


__all__ = ["HAS_MUTAGEN", "require"]
