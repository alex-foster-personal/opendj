"""State DB documentation: authored source of truth + generated per-machine copy.

Two-file pattern (specs/cloudsync-spec.md section 2, D3):
- ``apps/database/AGENTS.md`` -- hand-authored, git-tracked, includes the
  verbatim founding brief. Read this first.
- ``<data-root>/state/AGENTS.md`` -- generated next to the live DB by
  :mod:`apps.database.generate_agents_md`, merging sqlite introspection with
  the curated descriptions in :mod:`apps.database.column_docs`. Travels with
  the DB file so it cannot drift from a schema it never saw.

This package does NOT own the schema. ``apps/shared/state/schema.py``
remains the only schema authority (specs/cloudsync-spec.md D2); this package
only documents it, via :func:`regenerate_agents_md_if_writable` below and
the CLI at ``python -m apps.database.generate_agents_md``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

__all__ = ["regenerate_agents_md_if_writable"]


def regenerate_agents_md_if_writable(
    conn: sqlite3.Connection,
    state_dir: Path,
    *,
    owned_tables: frozenset[str] | None = None,
    cache_marker: str | None = None,
) -> bool:
    """Regenerate ``<state_dir>/AGENTS.md`` from ``conn``, if ``state_dir`` is writable.

    Called from ``apps.shared.state.db.open_rw`` after migrations and machine-id
    backfill succeed (local import there, not at module scope -- see
    ``apps/database/generate_agents_md.py``'s module docstring for the
    circular-import risk). ``target.parent`` in ``open_rw`` is the state
    directory both state.db and AGENTS.md live in.

    Returns False, writing nothing, when ``state_dir`` is not writable -- a
    read-only mount, a CI sandbox, a directory that does not exist yet.
    This is the ONE deliberate, spec-mandated skip ("the generator is
    invoked IF the data dir is writable"), checked BEFORE any attempt is
    made -- it is not a caught failure. Once writability is confirmed, any
    real error inside :func:`write_agents_md` (most notably
    :class:`apps.database.generate_agents_md.MissingColumnDocsError`, the
    drift guard) still propagates: migrations succeeding does not make a
    stale ``column_docs.py`` acceptable.

    ``owned_tables`` is forwarded to :func:`write_agents_md`. ``open_rw``
    passes ``schema.ALL_KNOWN_TABLES`` so leftover tables that are not on
    the current ladder cannot abort the open, while foreign-authority
    tables stay in the sidecar. The CLI and tests that omit it keep the
    strict every-live-table guard. ``cache_marker`` is forwarded too; the
    cache gate that computes it lives with its caller in
    :mod:`apps.shared.state.agents_md_cache`, so this package never imports
    ``apps.shared`` (that would make ``database <-> shared`` a package cycle).
    """
    if not os.access(state_dir, os.W_OK):
        return False
    # Local import, deliberately: eagerly importing generate_agents_md at
    # package scope makes `python -m apps.database.generate_agents_md`
    # load it twice under two different module identities (once via this
    # package's __init__, once as __main__) -- Python warns about exactly
    # this ("found in sys.modules ... prior to execution ... unpredictable
    # behaviour"). Deferring the import here, off the CLI's hot path,
    # avoids it.
    from apps.database.generate_agents_md import write_agents_md

    write_agents_md(
        conn, state_dir / "AGENTS.md", owned_tables=owned_tables, cache_marker=cache_marker
    )
    return True
