"""Public contract of ``apps.set_notes`` (ADR-NEW-module-contracts).

The package is reached from outside only through ``apps.set_notes.http`` (the
router and the schema entry point) and its CLI; it exports no shared types yet.
"""

from __future__ import annotations

CONTRACT_VERSION = "set_notes/1"
