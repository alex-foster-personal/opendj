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


def regenerate_agents_md_if_writable(conn: sqlite3.Connection, state_dir: Path) -> bool:
    """Regenerate ``<state_dir>/AGENTS.md`` from ``conn``, if ``state_dir`` is writable.

    WIRING ASK for whoever owns ``apps/shared/state/db.py`` (that module is
    a layer below this package, so it should import this locally rather
    than at module scope -- see ``apps/database/generate_agents_md.py``'s
    module docstring for why a top-level import the other way round would
    cycle): call this one line right after
    ``apps.shared.state.schema.apply_migrations(conn)`` succeeds inside
    ``open_rw``::

        from apps.database import regenerate_agents_md_if_writable
        regenerate_agents_md_if_writable(conn, target.parent)

    (``target`` is already the state.db path in ``open_rw``; ``target.parent``
    is the state directory both state.db and AGENTS.md live in.)

    Returns False, writing nothing, when ``state_dir`` is not writable -- a
    read-only mount, a CI sandbox, a directory that does not exist yet.
    This is the ONE deliberate, spec-mandated skip ("the generator is
    invoked IF the data dir is writable"), checked BEFORE any attempt is
    made -- it is not a caught failure. Once writability is confirmed, any
    real error inside :func:`write_agents_md` (most notably
    :class:`apps.database.generate_agents_md.MissingColumnDocsError`, the
    drift guard) still propagates: migrations succeeding does not make a
    stale ``column_docs.py`` acceptable.
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

    write_agents_md(conn, state_dir / "AGENTS.md")
    return True
