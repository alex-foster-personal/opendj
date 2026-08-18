"""Engine store: the single owner of the SQLite schema.

Additive during the rebuild -- nothing imports this yet. Integration
(pointing the daemon's connection factory at :func:`apply_migrations` and
deleting the ~17 scattered bootstraps) is a separate, later step.
"""
from __future__ import annotations

from .schema import (
    ALL_TABLES,
    DOMAINS,
    LEGACY_SOURCES,
    MIGRATIONS,
    SCHEMA_VERSION,
    TABLES,
    SchemaAdoptionError,
    apply_migrations,
    consolidated_version,
    ensure_vendor_sidecar_tables,
    was_adopted,
)

__all__ = [
    "ALL_TABLES",
    "DOMAINS",
    "LEGACY_SOURCES",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "TABLES",
    "SchemaAdoptionError",
    "apply_migrations",
    "consolidated_version",
    "ensure_vendor_sidecar_tables",
    "was_adopted",
]
