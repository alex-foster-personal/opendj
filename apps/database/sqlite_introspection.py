"""Shared sqlite_master helpers for the two AGENTS.md generators.

Both :mod:`apps.database.agents_md` (markdown table reference) and
:mod:`apps.database.generate_agents_md` (YAML sidecar) introspect the same
live file. They share sqlite-internal exclusion (``sqlite_%``) and live
``CREATE VIRTUAL TABLE`` discovery. They do not share a shadow filter:
``agents_md`` omits every ``<vtab>_*`` engine table (prefix), while
``generate_agents_md`` omits only the five fts5 suffixes. Callers pass
``is_shadow``; there is no default.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

# Suffixes SQLite attaches to an fts5 virtual table name (``<vtab>_data``,
# ``<vtab>_idx``, ...). They hold index internals, not application columns.
FTS5_SHADOW_SUFFIXES: tuple[str, ...] = (
    "_data",
    "_idx",
    "_docsize",
    "_config",
    "_content",
)


def virtual_table_names(conn: sqlite3.Connection) -> frozenset[str]:
    """Names of every ``CREATE VIRTUAL TABLE`` object in ``conn``."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL TABLE%'"
    ).fetchall()
    return frozenset(row[0] for row in rows)


def is_sqlite_internal_table(name: str) -> bool:
    return name.startswith("sqlite_")


def is_fts5_shadow_table(name: str, virtual_tables: frozenset[str]) -> bool:
    """True when ``name`` is fts5 engine storage, not an application table."""
    return any(name == f"{vtab}{suffix}" for vtab in virtual_tables for suffix in FTS5_SHADOW_SUFFIXES)


def is_virtual_table_shadow(name: str, virtual_tables: frozenset[str]) -> bool:
    """True when ``name`` is engine storage for a virtual table (``<vtab>_*``).

    Prefix match, not exact fts5 suffixes. The virtual table itself is not a
    shadow: ``tracks_fts`` does not start with ``tracks_fts_``.
    """
    if not virtual_tables:
        return False
    prefixes = tuple(f"{vtab}_" for vtab in virtual_tables)
    return name.startswith(prefixes)


def documentable_table_names(
    conn: sqlite3.Connection,
    *,
    owned_tables: frozenset[str] | None = None,
    is_shadow: Callable[[str, frozenset[str]], bool],
) -> list[str]:
    """Live table names to document, excluding sqlite internals and shadows.

    ``is_shadow`` is required (no hidden default). Pass
    :func:`is_virtual_table_shadow` (prefix) or :func:`is_fts5_shadow_table`
    (exact fts5 suffixes).

    When ``owned_tables`` is set, names outside that set are omitted (the
    ``open_rw`` path that passes ``schema.ALL_KNOWN_TABLES``).
    """
    virtual_tables = virtual_table_names(conn)
    names = [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        if not is_sqlite_internal_table(row[0]) and not is_shadow(row[0], virtual_tables)
    ]
    if owned_tables is not None:
        names = [name for name in names if name in owned_tables]
    return names


__all__ = [
    "FTS5_SHADOW_SUFFIXES",
    "documentable_table_names",
    "is_fts5_shadow_table",
    "is_sqlite_internal_table",
    "is_virtual_table_shadow",
    "virtual_table_names",
]
