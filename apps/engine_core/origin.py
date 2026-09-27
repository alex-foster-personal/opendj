"""Re-exports of the engine lock-file contract (AGENT-05, issue #2942).

The reader moved to :mod:`apps.shared.engine_origin` (PR #3831), because
``apps.sync_hub`` also needs it and ``apps.engine_core`` already imports from
``apps.sync_hub`` for its own app-lifespan wiring -- keeping the reader here
would have made ``sync_hub`` import back from ``engine_core``, closing a
package cycle. Every name keeps this import path, so nothing else in this
package's callers moves with it; delete this module once no importer needs
the old path.

A rebound name is not the same object as the module attribute a function
reads at call time: ``verify_engine_identity`` (defined in
``apps.shared.engine_origin``) resolves ``httpx`` and every module-level
constant from ITS OWN globals, not this shim's. Monkeypatching
``apps.engine_core.origin.DEFAULT_LOCK_PATH`` (or ``.httpx``, or any other
name re-exported here) silently patches nothing real -- target
``apps.shared.engine_origin`` directly instead (claude-review, PR #3831, P3).
"""

from __future__ import annotations

from apps.shared.engine_origin import (
    DEFAULT_LOCK_PATH,
    EXPECTED_ENGINE_ROLE,
    HEALTH_PATH,
    IDENTITY_PROBE_TIMEOUT_S,
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
    "EXPECTED_ENGINE_ROLE",
    "HEALTH_PATH",
    "IDENTITY_PROBE_TIMEOUT_S",
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
