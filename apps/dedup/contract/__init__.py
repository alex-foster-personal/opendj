"""The public surface of ``apps.dedup`` (ADR-NEW-module-contracts, M1).

Code outside ``apps.dedup`` imports only this package (and its submodules);
delivery layers and scripts may also import :mod:`apps.dedup.http`. This
module imports only the standard library, so the stdlib-only sync-drift gate
(``scripts.sync_drift_subject``) can reach the schema through it; the pydantic
models and the service Protocol live in :mod:`apps.dedup.contract.models`.
"""

from __future__ import annotations

from .schema import SCHEMA_SQL, ensure_schema

CONTRACT_VERSION = "dedup/1"

# Cluster bounds the review routes re-check before an apply; the clustering
# pass (apps.dedup.find_clusters) uses the same values as its defaults.
DEFAULT_MAX_CLUSTER = 8
DEFAULT_DURATION_DELTA_S = 3.0


__all__ = [
    "CONTRACT_VERSION",
    "DEFAULT_DURATION_DELTA_S",
    "DEFAULT_MAX_CLUSTER",
    "SCHEMA_SQL",
    "ensure_schema",
]
