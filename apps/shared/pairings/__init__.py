"""Pairing-memory graph (CAT-03).

Hand-curated (for v1) directed edges between tracks: "A flows into B",
"A and B flow either way". The table shape matches the open-dj v0 strawman
`Pairing` entity (``docs/open-dj-v0-strawman.md`` §4.7) so Phase 15 export
is a plain ``SELECT * FROM pairings`` + JSON serialisation.

Public API:
  * :class:`PairingEdge` -- frozen dataclass mirroring one row.
  * :class:`PairingsRepo` -- add / remove / list / get_neighbors / exists.

Schema lives in :mod:`apps.shared.state.schema` migration v2.
"""
from __future__ import annotations

from .capture_repo import Alignment, PairingCaptureError, PairingCaptureRepo, SyncSnapshot
from .models import PairingEdge
from .repo import PairingsError, PairingsRepo
from .schema_sql import apply_pairing_capture_migrations, ensure_phase08_tables

__all__ = [
    "Alignment",
    "PairingCaptureError",
    "PairingCaptureRepo",
    "PairingEdge",
    "PairingsError",
    "PairingsRepo",
    "SyncSnapshot",
    "apply_pairing_capture_migrations",
    "ensure_phase08_tables",
]
