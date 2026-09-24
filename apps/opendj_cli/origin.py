"""Re-exports of the engine lock-file contract (AGENT-05, issue #2751).

The reader moved to :mod:`apps.engine_core.origin`, beside the writer, so that
``apps.engine_core`` no longer imports ``apps.opendj_cli`` and the two packages
stop forming an import cycle (issue #2942). It moved again, to
:mod:`apps.shared.engine_origin`, when ``apps.sync_hub`` also needed it (PR
#3831) -- ``apps.engine_core.origin`` re-exports it in turn, so this module's
own import path is unaffected. Every name keeps this import path, so nothing
else in the CLI moves with it; delete this module once no importer needs the
old path.
"""

from __future__ import annotations

from apps.engine_core.origin import (
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
