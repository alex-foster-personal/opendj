"""Public contract of ``apps.tags`` (ADR-NEW-module-contracts).

``apps.tags`` is run as a command (``python -m apps.tags...``) and no other package
imports it, so this contract offers no operations yet. Code outside the package
imports only this module; the first operation another package needs is declared
here, as a Protocol over frozen models, before anything imports it.
"""

from __future__ import annotations

CONTRACT_VERSION = "tags/1"
