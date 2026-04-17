"""Pioneer USB export reader + writer (CAT-06).

Public surface
--------------

Prototype A (reader) — when ``reader.py`` is present, re-exports
:func:`read_usb_export`. Absence is tolerated so downstream callers can
still import this package.

Prototype B (writer) — :func:`writer_rbox.write_onelibrary`: serialise a
minimal tracks-and-playlists payload into an ``exportLibrary.db``
OneLibrary (Device Library Plus) file via the ``rbox`` Rust crate's
Python bindings. See ``writer_rbox.py`` for the capability matrix
documenting what rbox 0.1.7 can and cannot do (spoiler: fresh-create is
buggy; seed-from-fixture + modify works).

Requirement: CAT-06.
"""
from __future__ import annotations

__all__: list[str] = []

# Prototype A reader is optional — its Kaitai-vendored parser lives in
# ``reader.py`` which may be absent in some worktrees.
try:  # pragma: no cover - import guard
    from .reader import read_usb_export  # type: ignore[import-not-found]

    __all__.append("read_usb_export")
except Exception:  # noqa: BLE001 — any import failure is non-fatal here.
    pass
