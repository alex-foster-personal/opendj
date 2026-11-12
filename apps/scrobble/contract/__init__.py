"""Public contract of ``apps.scrobble`` (ADR-NEW-module-contracts).

ListenBrainz payload building and play submission. Code outside the package
imports only this module. Nothing outside the package imports it yet.
"""

from __future__ import annotations

CONTRACT_VERSION = "scrobble/1"
