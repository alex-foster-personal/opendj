"""Public contract of ``apps.pairings`` (ADR-NEW-module-contracts).

``apps.pairings`` is run as a command (``python -m apps.pairings...``) and no other package
imports it, so this contract offers no operations yet. Code outside the package
imports only this module; the first operation another package needs is declared
here, as a Protocol over frozen models, before anything imports it.
"""

from __future__ import annotations

CONTRACT_VERSION = "pairings/1"
