"""Compatibility facade for shared SQLite introspection helpers."""

from apps.shared.agents_md.sqlite_introspection import (
    FTS5_SHADOW_SUFFIXES,
    documentable_table_names,
    is_fts5_shadow_table,
    is_sqlite_internal_table,
    is_virtual_table_shadow,
    virtual_table_names,
)

__all__ = [
    "FTS5_SHADOW_SUFFIXES",
    "documentable_table_names",
    "is_fts5_shadow_table",
    "is_sqlite_internal_table",
    "is_virtual_table_shadow",
    "virtual_table_names",
]
