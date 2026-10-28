"""Public contract of ``apps.streaming_transfer`` (ADR-NEW-module-contracts).

``apps.streaming_transfer`` is run as a command (``python -m apps.streaming_transfer...``) and no other package
imports it, so this contract offers no operations yet. Code outside the package
imports only this module; the first operation another package needs is declared
here, as a Protocol over frozen models, before anything imports it.
"""

from __future__ import annotations

CONTRACT_VERSION = "streaming_transfer/1"
