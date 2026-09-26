"""Cache gate for the post-migration AGENTS.md regeneration (issue #4015).

Every ``open_rw`` on an unchanged schema used to re-run full sqlite
introspection plus YAML rendering on the request thread: 80 of 513 py-spy
samples in a 10k-row listing walk. :func:`regenerate_agents_md_cached` skips
that when BOTH halves of the cache agree:

- a ``schema_meta_markers`` row keyed on
  :func:`apps.database.generate_agents_md.agents_md_cache_marker` (sqlite's
  own ``PRAGMA schema_version``, the owned tables, and the generator
  version), and
- the sidecar beside this DB ending with that marker's cache line (a tail
  read). The row travels with a restored or copied state.db and the file
  does not, so the row alone cannot prove the sidecar is current.

A miss runs the real generator (``MissingColumnDocsError`` still raises
before any write) and then records the marker idempotently: two opens that
both miss must not abort on the primary key.

The gate lives here, beside its only production caller ``open_rw``, rather
than in ``apps.database``: ``apps.shared`` already imports ``apps.database``,
and the reverse import would make the two packages a cycle.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.shared.state import schema_markers


def regenerate_agents_md_cached(
    conn: sqlite3.Connection,
    state_dir: Path,
    *,
    owned_tables: frozenset[str] | None,
) -> bool:
    """Regenerate ``<state_dir>/AGENTS.md`` unless the cache proves it current.

    Returns True when it wrote the sidecar, and False on a cache hit or an
    unwritable ``state_dir`` (see
    :func:`apps.database.regenerate_agents_md_if_writable`).
    """
    # Local imports for the same reason open_rw defers apps.database: see
    # apps/database/generate_agents_md.py's module docstring.
    from apps.database import regenerate_agents_md_if_writable
    from apps.database.generate_agents_md import (
        GENERATOR_VERSION,
        agents_md_cache_marker,
        sidecar_carries_cache_line,
    )

    marker = agents_md_cache_marker(
        sqlite_schema_version=conn.execute("PRAGMA schema_version").fetchone()[0],
        owned_tables=owned_tables,
        generator_version=GENERATOR_VERSION,
    )
    if schema_markers.has_marker(conn, marker) and sidecar_carries_cache_line(
        state_dir / "AGENTS.md", marker
    ):
        return False
    wrote = regenerate_agents_md_if_writable(
        conn, state_dir, owned_tables=owned_tables, cache_marker=marker
    )
    if wrote and schema_markers.table_exists(conn, schema_markers.MARKER_TABLE):
        schema_markers.insert_marker_if_absent(conn, marker)
    return wrote


__all__ = ["regenerate_agents_md_cached"]
