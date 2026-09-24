"""Re-exports of the engine lock-file contract (AGENT-05, issue #2942).

The reader moved to :mod:`apps.shared.engine_origin` (PR #3831), because
``apps.sync_hub`` also needs it and ``apps.engine_core`` already imports from
``apps.sync_hub`` for its own app-lifespan wiring -- keeping the reader here
would have made ``sync_hub`` import back from ``engine_core``, closing a
package cycle. Every name keeps this import path, so nothing else in this
package's callers moves with it; delete this module once no importer needs
the old path.
"""

from __future__ import annotations

from apps.shared.engine_origin import (
    DEFAULT_LOCK_PATH,
    LOCK_PATH_ENV,
    EngineIdentityMismatch,
    EngineNotRunning,
    EngineOrigin,
    lock_document,
    lock_path,
    resolve_origin,
    resolve_verified_origin,
    unreachable,
    verify_engine_identity,
)

__all__ = [
    "DEFAULT_LOCK_PATH",
    "LOCK_PATH_ENV",
    "EngineIdentityMismatch",
    "EngineNotRunning",
    "EngineOrigin",
    "lock_document",
    "lock_path",
    "resolve_origin",
    "resolve_verified_origin",
    "unreachable",
    "verify_engine_identity",
]
