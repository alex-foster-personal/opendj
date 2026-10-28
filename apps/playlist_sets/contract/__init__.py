"""Public contract of ``apps.playlist_sets`` (ADR-NEW-module-contracts).

``apps.playlist_sets`` is run as a command (``python -m apps.playlist_sets...``) and no other package
imports it, so this contract offers no operations yet. Code outside the package
imports only this module; the first operation another package needs is declared
here, as a Protocol over frozen models, before anything imports it.
"""

from __future__ import annotations

CONTRACT_VERSION = "playlist_sets/1"
