"""Reuse a CloudSync digest while nothing it reads has changed (issue #4396).

A no-op sync used to re-hash every row of every digested table on both
machines (issue #2899, H1). :func:`gated_digest` skips that walk when every
input the walk reads is provably the one a previous walk saw, and walks in
full whenever that cannot be proven.

The proof is a :class:`DigestInputs` read in the caller's transaction:

* the ``local_changelog`` and ``hub_changelog`` seqs, the changelog gate
  the issue asks for;
* one write token per digested table
  (:mod:`apps.shared.state.migrations_v20`), replaced by a trigger on every
  row write, so a write that bypassed the changelog still moves the key. The
  changelog alone cannot prove "unchanged": it is appended by application
  code, and the digest exists precisely to catch the writes that code missed;
* ``PRAGMA schema_version``, because the column list is part of each hash;
* the whole ``sync_identity_remap`` table, which decides which ``tracks``
  rows are held and has no triggers of its own (it is created lazily).

FAIL SAFE: when any of that cannot be read as expected (a trigger missing or
altered, a token row absent) :func:`read_inputs` answers ``None`` and the
digest is walked, with one WARNING per process naming why. A cache entry is
stored only when the inputs read after the walk equal those read before it,
so a writer landing mid-walk can never file a digest under the wrong key.

The cache is per process and bounded; a restart walks once and refills it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from apps.shared.state import migrations_v20
from apps.sync_hub import sync_set

if TYPE_CHECKING:
    from apps.sync_hub.protocol import SyncDigest

log = logging.getLogger(__name__)

# ----- config -------------------------------------------------------------------


class CFG:
    #: :data:`apps.sync_hub.engine_identity_map.REMAP_TABLE`, spelled here
    #: because that module imports :mod:`apps.sync_hub.protocol`, which
    #: imports this one. Pinned equal by tests/cloudsync/test_digest_write_gate.py.
    IDENTITY_REMAP_TABLE: str = "sync_identity_remap"
    #: Digests kept per process: a hub and a spoke engine each need one; tests
    #: open many DBs in one process, so the bound keeps them from accumulating.
    MAX_ENTRIES: int = 8


# ----- inputs -------------------------------------------------------------------


@dataclass(frozen=True)
class DigestInputs:
    """Everything a digest walk reads, reduced to values cheap to compare."""

    changelog_seqs: tuple[int, int]
    schema_cookie: int
    write_tokens: tuple[tuple[str, bytes], ...]
    identity_remap: tuple[tuple[object, ...], ...] | None


_warned: set[str] = set()


def _unprovable(reason: str) -> None:
    """Say once per process why the gate is walking every digest in full."""
    if reason not in _warned:
        _warned.add(reason)
        log.warning("CloudSync digest gate disabled, walking in full: %s", reason)


def _trigger_fault(conn: sqlite3.Connection) -> str | None:
    """Why the write-token triggers cannot be trusted, or None when intact."""
    stored = dict(
        conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").fetchall()
    )
    for name, sql in migrations_v20.EXPECTED_TRIGGERS.items():
        if stored.get(name) != sql:
            return f"trigger {name} is missing or altered"
    return None


def _write_tokens(conn: sqlite3.Connection) -> tuple[tuple[str, bytes], ...] | None:
    """One token per digested table, or None when any is absent."""
    tokens = dict(
        conn.execute(
            f"SELECT table_name, token FROM {migrations_v20.WRITE_TOKEN_TABLE}"
        ).fetchall()
    )
    if not set(sync_set.FK_ORDER) <= set(tokens):
        return None
    return tuple((table, bytes(tokens[table])) for table in sync_set.FK_ORDER)


def _identity_remap(conn: sqlite3.Connection) -> tuple[tuple[object, ...], ...] | None:
    """Every stored remap row; None while the lazily created table is absent."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (CFG.IDENTITY_REMAP_TABLE,),
    ).fetchone()
    if exists is None:
        return None
    return tuple(
        tuple(row) for row in conn.execute(f"SELECT * FROM {CFG.IDENTITY_REMAP_TABLE} ORDER BY loser_pk")
    )


def _max_seq(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COALESCE(MAX(seq), 0) FROM {table}").fetchone()[0])


def read_inputs(conn: sqlite3.Connection) -> DigestInputs | None:
    """The gate key for ``conn``'s current snapshot, or None when unprovable."""
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if migrations_v20.WRITE_TOKEN_TABLE not in tables:
        _unprovable(f"{migrations_v20.WRITE_TOKEN_TABLE} is absent (schema below v20)")
        return None
    fault = _trigger_fault(conn)
    if fault is not None:
        _unprovable(fault)
        return None
    tokens = _write_tokens(conn)
    if tokens is None:
        _unprovable(f"{migrations_v20.WRITE_TOKEN_TABLE} lacks a digested table's row")
        return None
    return DigestInputs(
        changelog_seqs=(_max_seq(conn, "local_changelog"), _max_seq(conn, "hub_changelog")),
        schema_cookie=int(conn.execute("PRAGMA schema_version").fetchone()[0]),
        write_tokens=tokens,
        identity_remap=_identity_remap(conn),
    )


# ----- cache --------------------------------------------------------------------


_cache: OrderedDict[DigestInputs, SyncDigest] = OrderedDict()
_cache_lock = threading.Lock()


def _lookup(key: DigestInputs) -> SyncDigest | None:
    with _cache_lock:
        found = _cache.get(key)
        if found is not None:
            _cache.move_to_end(key)
        return found


def _store(key: DigestInputs, digest: SyncDigest) -> None:
    with _cache_lock:
        _cache[key] = digest
        _cache.move_to_end(key)
        while len(_cache) > CFG.MAX_ENTRIES:
            _cache.popitem(last=False)


def _copy_at(digest: SyncDigest, seq: int) -> SyncDigest:
    """A caller's own copy: the cached maps must never be shared mutably."""
    return replace(
        digest,
        seq=seq,
        tables=dict(digest.tables),
        quarantined=None if digest.quarantined is None else dict(digest.quarantined),
        hash_pending=None if digest.hash_pending is None else dict(digest.hash_pending),
    )


def gated_digest(
    conn: sqlite3.Connection, seq: int, walk: Callable[[], SyncDigest]
) -> SyncDigest:
    """The cached digest when the inputs prove it current, else ``walk()``."""
    before = read_inputs(conn)
    if before is not None:
        cached = _lookup(before)
        if cached is not None:
            return _copy_at(cached, seq)
    digest = walk()
    if before is not None and read_inputs(conn) == before:
        _store(before, _copy_at(digest, seq))
    return digest


def reset_for_tests() -> None:
    """Forget every cached digest and every warning this process gave."""
    with _cache_lock:
        _cache.clear()
    _warned.clear()


__all__ = ["CFG", "DigestInputs", "gated_digest", "read_inputs", "reset_for_tests"]
