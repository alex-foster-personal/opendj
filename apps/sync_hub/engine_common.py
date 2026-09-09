"""Shared kernel for the merge engine split: errors, changelog constants,
the apply-order index, and the pk-predicate builder.

Split out of :mod:`apps.sync_hub.engine` (quality-gate file_size ratchet,
round 4): ``engine.py`` crossed 600 lines and the boundaries were already
named by its own section comments (watermarks, machines, push, apply, pull,
retention). Everything here has no connection-reading logic of its own --
it exists so the other engine submodules can depend on ONE shared module
instead of on each other, keeping the split acyclic.
"""
from __future__ import annotations

from apps.shared.state.sync_stamp import LOCAL_CHANGELOG_TABLE
from apps.sync_hub.protocol import SPEC_BY_TABLE, SYNC_TABLES, TableSpec

HUB_CHANGELOG_TABLE: str = "hub_changelog"

#: The two changelog tables :func:`apps.sync_hub.engine_retention.prune_changelog`
#: will touch. An allowlist because the table name is interpolated into the
#: DELETE -- SQLite does not bind identifiers, so this check is load-bearing,
#: not decorative.
CHANGELOG_TABLES: frozenset[str] = frozenset({HUB_CHANGELOG_TABLE, LOCAL_CHANGELOG_TABLE})

#: Retention defaults for :func:`apps.sync_hub.engine_retention.prune_changelog`.
#: Generous on purpose: the prune is lossless for the pull (it only drops
#: superseded entries), so the bounds exist to keep a recent audit trail, not
#: to protect correctness.
DEFAULT_KEEP_DAYS: float = 30.0
DEFAULT_KEEP_ROWS: int = 10_000

#: Hub-side cap on one ``/pull`` response. A first sync of a real library is
#: 6.6 MB+ of JSON (round 1 finding A2), held twice in memory on each side;
#: the client loops until the hub reports no more.
DEFAULT_PULL_LIMIT: int = 500


class SyncApplyError(RuntimeError):
    """A row could not be applied. Loud by design; sync stops here."""


class SyncSchemaMismatch(SyncApplyError):
    """A peer offered a row whose columns are not this DB's columns."""


#: Parents-before-children order for a batch of mixed-table changes; the
#: chunking in :mod:`apps.sync_hub.client` depends on it (a chunk boundary
#: must never put a child row in an earlier request than its parent).
_APPLY_ORDER: dict[str, int] = {
    spec.name: index for index, spec in enumerate(SYNC_TABLES)
}


def apply_rank(table: str, *, source: str) -> int:
    """This table's parents-before-children rank, or a loud refusal.

    Every parents-first sort in the engine reads ``_APPLY_ORDER`` through a
    ``sorted(..., key=...)`` lambda, and ``sorted`` evaluates its key for
    EVERY element before the loop body runs once. A bare ``_APPLY_ORDER[...]``
    in that lambda therefore raises ``KeyError`` out of the sort for an
    out-of-set table name, ahead of any ``spec is None`` guard written beneath
    the loop -- so the guard that carries the useful message is unreachable
    for exactly the input it was written for, and callers that translate
    ``SyncApplyError`` into a 409 answer an unhandled 500 instead.

    ``local_changelog.table_name`` is a bare ``TEXT NOT NULL``, so the input
    is storable rather than hypothetical: a writer for a table outside the
    sync set, a hand-repaired row, or a downgrade past a schema that added a
    table all produce it.

    ``source`` names WHERE the bad name was read, which is what tells an
    operator which log to go and repair.
    """
    try:
        return _APPLY_ORDER[table]
    except KeyError:
        raise SyncApplyError(
            f"{source} references table {table!r}, which is not in the sync "
            f"set; the sync set is {sorted(SPEC_BY_TABLE)}"
        ) from None


def _pk_predicate(spec: TableSpec) -> str:
    return " AND ".join(f"{column} = ?" for column in spec.pk)


__all__ = [
    "CHANGELOG_TABLES",
    "DEFAULT_KEEP_DAYS",
    "DEFAULT_KEEP_ROWS",
    "DEFAULT_PULL_LIMIT",
    "HUB_CHANGELOG_TABLE",
    "SyncApplyError",
    "SyncSchemaMismatch",
    "apply_rank",
]
