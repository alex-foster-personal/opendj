"""Pioneer USB export reader + writer (CAT-06).

Public surface
--------------

Prototype A (reader) — when ``reader.py`` is present, re-exports
:func:`read_usb_export`. Absence is tolerated so downstream callers can
still import this package.

Prototype B (writer) — :func:`writer_onelibrary.write_onelibrary`: serialise a
minimal tracks-and-playlists payload into an ``exportLibrary.db``
OneLibrary (Device Library Plus) file, seeded from a Rekordbox-made
template, through our own SQLCipher handle (``onelibrary.py``).

Requirement: CAT-06.
"""
from __future__ import annotations

__all__ = ["read_usb_export"]


def __getattr__(name: str):
    if name == "read_usb_export":
        from .reader import read_usb_export as _read_usb_export

        return _read_usb_export
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
