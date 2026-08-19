"""First-run setup: detect what is installed, import it, report honestly.

Three modules, three jobs:

* :mod:`detect`   -- reads what exists on this machine WITHOUT opening it.
* :mod:`importer` -- the snapshot -> decrypt -> ingest -> analysis pipeline.
* :mod:`api`      -- the HTTP surface, one endpoint per wizard step.

:mod:`worker` is the subprocess the engine's job runner spawns; it is the
ONLY thing that speaks the JSON-lines progress protocol.

Every refusal in here carries a machine-readable code from
:data:`detect.CODES`. "rekordbox is not installed", "the SQLCipher key is
unreachable" and "the decrypt itself failed" are three different problems
with three different fixes, so they are never collapsed into one message.
"""

from __future__ import annotations

__all__ = ["api", "detect", "importer", "jobs", "record"]
