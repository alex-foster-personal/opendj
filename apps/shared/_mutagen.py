"""Single import-guard for the optional ``mutagen`` tag-WRITING dependency.

Why this exists
---------------

``mutagen`` is GPL-2.0-or-later whereas music-dj-tools ships under Apache-2.0.
To keep the Apache wheel ``pip install``-clean of a GPL *runtime* requirement
(see LIC-1 in the dep audit) we demoted ``mutagen`` from ``[project]
.dependencies`` to the ``[project.optional-dependencies].tags`` extra.

Since Thu 1 Oct 2026 every tag READ goes through ``tinytag`` (MIT, core;
see :mod:`apps.shared._tagreader`). What remains here is the opt-in write
family: ``apps.shared.tag_writer`` (and the ``apps.tags`` unify pipeline
built on it), ``apps.analysis.write_tags`` and the Serato GEOB codec.

Every such callsite imports from this module and invokes
:func:`require` at the public-function entry point, so:

* Modules can still be imported without ``mutagen`` present; tests,
  fingerprint probes, and metadata fallbacks keep working.
* Any caller that actually touches audio tags gets a clear, actionable
  :class:`ImportError` pointing at the ``pip install music-dj-tools[tags]``
  remediation path.

The helper is deliberately tiny: no side effects at import time, one public
sentinel (:data:`HAS_MUTAGEN`), one raiser (:func:`require`).
"""
from __future__ import annotations

try:  # pragma: no cover - trivially exercised at import time
    import mutagen as _mutagen  # type: ignore
    HAS_MUTAGEN: bool = True
except ImportError:  # pragma: no cover - only on installs without [tags]
    _mutagen = None  # type: ignore[assignment]
    HAS_MUTAGEN = False


_INSTALL_HINT = (
    "tag read/write requires the optional 'mutagen' dependency. "
    "Install with: pip install 'music-dj-tools[tags]' "
    "(mutagen is GPL-2.0-or-later and therefore shipped as an opt-in extra)."
)


def require() -> None:
    """Raise :class:`ImportError` with an install hint when mutagen is absent.

    Callers invoke this at the top of any public tag I/O function so users
    hit a clean, self-describing error instead of the opaque ``from mutagen
    import ...`` traceback further down the call stack.
    """
    if not HAS_MUTAGEN:
        raise ImportError(_INSTALL_HINT)


__all__ = ["HAS_MUTAGEN", "require"]
