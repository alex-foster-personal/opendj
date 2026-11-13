"""Cache gate for the post-migration AGENTS.md regeneration (issue #4015).

Every ``open_rw`` on an unchanged schema used to re-run full sqlite
introspection plus YAML rendering on the request thread: 80 of 513 py-spy
samples in a 10k-row listing walk. :func:`regenerate_agents_md_cached` skips
that when BOTH halves of the cache agree:

- a ``schema_meta_markers`` row keyed on
  :func:`apps.shared.agents_md.generator.agents_md_cache_marker` (sqlite's
  own ``PRAGMA schema_version``, a content hash of ``sqlite_master``, the
  owned tables, and the generator version), and
- the sidecar beside this DB ending with that marker's cache line (a tail
  read). The row travels with a restored or copied state.db and the file
  does not, so the row alone cannot prove the sidecar is current.

A miss runs the real generator and then records the marker idempotently:
two opens that both miss must not abort on the primary key. A
``MissingColumnDocsError`` there (it raises before any write) is logged and
skipped, not raised, so the open continues and no marker is recorded.

The gate lives here beside its only production caller ``open_rw``; the
generator implementation lives in :mod:`apps.shared.agents_md` so this
stable-core path has no dependency on :mod:`apps.database`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from apps.shared.state import schema_markers

_log = logging.getLogger(__name__)

#: Cache markers whose schema failed column-docs coverage in this process.
#: Neither half of the cache can record them (the sidecar is left as is), so
#: without this every later ``open_rw`` would re-run introspection and log
#: the same warning again. The marker carries the schema fingerprint, so a
#: schema change still gets a fresh attempt.
_UNDOCUMENTED_SCHEMA_MARKERS: set[str] = set()


def _capture_sidecar_write_failure(exc: BaseException) -> None:
    """Report sidecar persistence failures when Sentry is available."""
    try:
        import sentry_sdk
    except ImportError:
        return
    if sentry_sdk.get_client() is None:
        return
    sentry_sdk.capture_exception(exc)


def _regenerate_agents_md_tolerating_sidecar_failures(
    conn: sqlite3.Connection,
    state_dir: Path,
    *,
    owned_tables: frozenset[str] | None,
    marker: str,
) -> bool:
    """Regenerate the doc sidecar; never abort ``open_rw`` on it.

    Expected sharing-lock read/replace denials log at warning. Other
    ``OSError`` during regeneration log at error and may reach Sentry, but
    still return False so the database open continues and a later open retries.

    A ``MissingColumnDocsError`` logs one warning naming the columns and
    leaves the existing sidecar as is. On a user's engine it means a newer
    build already added columns to this shared data dir (Thu 12 Nov 2026:
    ``playlist_memberships.edit_id`` stopped an older build starting). The
    drift guard stays strict where developers see it: the generator CLI,
    ``write_agents_md`` itself, and the coverage tests in
    ``(private path)``.
    """
    from apps.shared.agents_md.generator import (
        ExistingSidecarAccessError,
        MissingColumnDocsError,
        SidecarReplacePermissionError,
        write_agents_md,
    )

    try:
        if not os.access(state_dir, os.W_OK):
            return False
        write_agents_md(
            conn,
            state_dir / "AGENTS.md",
            owned_tables=owned_tables,
            cache_marker=marker,
        )
    except (ExistingSidecarAccessError, SidecarReplacePermissionError) as exc:
        _log.warning(
            "AGENTS.md sidecar access failed (%s); skipping this open",
            exc,
        )
        return False
    except MissingColumnDocsError as exc:
        _log.warning(
            "AGENTS.md not regenerated: %d undocumented live column(s), likely "
            "added by a newer build sharing this data dir; keeping the existing "
            "file: %s",
            len(exc.missing),
            ", ".join(exc.missing),
        )
        _UNDOCUMENTED_SCHEMA_MARKERS.add(marker)
        return False
    except OSError as exc:
        _log.error(
            "AGENTS.md regeneration persistence failed (errno=%s path=%s): %s",
            exc.errno,
            getattr(exc, "filename", None),
            exc,
        )
        _capture_sidecar_write_failure(exc)
        return False
    else:
        return True


def regenerate_agents_md_cached(
    conn: sqlite3.Connection,
    state_dir: Path,
    *,
    owned_tables: frozenset[str] | None,
) -> bool:
    """Regenerate ``<state_dir>/AGENTS.md`` unless the cache proves it current.

    Returns True when it wrote the sidecar, and False on a cache hit or an
    unwritable ``state_dir``.
    """
    from apps.shared.agents_md.generator import (
        GENERATOR_VERSION,
        agents_md_cache_marker,
        schema_fingerprint,
        sidecar_carries_cache_line,
    )

    marker = agents_md_cache_marker(
        sqlite_schema_version=conn.execute("PRAGMA schema_version").fetchone()[0],
        schema_fingerprint=schema_fingerprint(conn),
        owned_tables=owned_tables,
        generator_version=GENERATOR_VERSION,
    )
    if marker in _UNDOCUMENTED_SCHEMA_MARKERS:
        return False
    sidecar_path = state_dir / "AGENTS.md"
    if schema_markers.has_marker(conn, marker):
        try:
            sidecar_current = sidecar_carries_cache_line(sidecar_path, marker)
        except OSError as exc:
            # Windows file-sharing locks (OPEN-DJ-BE-A6) and other transient
            # read denials cannot prove the sidecar current; try regen below.
            _log.warning(
                "AGENTS.md sidecar cache read failed (%s); treating as cache miss",
                exc,
            )
        else:
            if sidecar_current:
                return False
    wrote = _regenerate_agents_md_tolerating_sidecar_failures(
        conn,
        state_dir,
        owned_tables=owned_tables,
        marker=marker,
    )
    if wrote and schema_markers.table_exists(conn, schema_markers.MARKER_TABLE):
        _record_marker_without_waiting(conn, marker)
    return wrote


def _record_marker_without_waiting(conn: sqlite3.Connection, marker: str) -> None:
    """Record ``marker`` only if the writer lock is free right now.

    The row is a cache hint, and recording it is the only write this gate
    adds to ``open_rw``. Waiting for the writer lock there would make every
    open that follows a schema change queue behind whichever peer holds it,
    up to the caller's busy timeout, and then fail the open outright: a
    request's own write would land after a concurrent one it used to precede.
    So the insert runs with a zero busy timeout, and SQLITE_BUSY skips it.
    The sidecar is already current, and the next open that misses records the
    row. Any other error still raises.
    """
    (busy_timeout_ms,) = conn.execute("PRAGMA busy_timeout").fetchone()
    conn.execute("PRAGMA busy_timeout = 0")
    try:
        schema_markers.insert_marker_if_absent(conn, marker)
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY:
            raise
    finally:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")


__all__ = ["regenerate_agents_md_cached"]
