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
from apps.sync_hub.protocol import SYNC_TABLES, TableSpec

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
]
