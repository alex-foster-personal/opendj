"""Merge engine: push selection, last-writer-wins apply, changelog, watermarks.

Contract: ``specs/design_decision_04.md``. Nothing in this module stamps a
timestamp onto a domain row -- rows carry the ``updated_at`` /
``origin_device_id`` their originating machine wrote, and that pair is the
only thing the merge looks at. The single clock this module reads is
``hub_changelog.received_at``, which is hub bookkeeping and never syncs.

Four behaviours that look surprising until you know why:

* **Push selection is a sequence fence, never a clock** (ADR 08 point 3,
  round 1 finding 3). A spoke offers the rows its own ``local_changelog``
  recorded above ``sync_state.last_push_seq``. The round 1 push floor was a
  wall clock maximum taken over rows the machine had just *pulled*, so a
  single peer with a skewed clock could raise the floor past every local
  edit and those edits were never offered again -- not rejected, never sent.
  ``updated_at`` now resolves conflicts and nothing else.
* **A spoke that has never completed a sync against this peer offers
  everything.** Migration v6 cannot retro-log the rows that already existed,
  so there is no changelog entry to fence against; ``last_sync_at`` records
  that a full offer has happened at least once. It is a completion marker,
  not a watermark: nothing ever compares it to a row's ``updated_at``.
* **``track_locations`` resolves on its natural key, not its primary key**
  (ADR 08 point 1). ``location_id`` is a random uuid minted per machine, so
  the same logical row can arrive under a key this DB has never seen; keying
  the upsert on ``location_id`` alone turned that into a UNIQUE violation,
  an HTTP 409, and a spoke that could never sync again (finding 1). The
  duplicate is resolved by LWW like any other conflict, with the smaller
  ``location_id`` as the tiebreak so both peers converge on the same one.
* **A losing playlist row discards its membership bundle.** Membership is
  whole-playlist (ADR 04 c5): it replaces the peer's copy only when the
  ``playlists`` row itself wins, so a reorder and an add in the same window
  resolve to one of them, not to an interleaving of both.

Split into submodules by concern (quality-gate file_size ratchet, round 4;
the boundaries were already named by this file's own section comments):
:mod:`apps.sync_hub.engine_common` (errors, changelog constants, apply
order), :mod:`apps.sync_hub.engine_watermark` (watermarks and sequences),
:mod:`apps.sync_hub.engine_machines` (the machines registry merge),
:mod:`apps.sync_hub.engine_changes` (push selection and one pull chunk),
:mod:`apps.sync_hub.engine_apply` (last-writer-wins apply), and
:mod:`apps.sync_hub.engine_retention` (changelog pruning). This module
re-exports all of it, so every caller that already does
``from apps.sync_hub import engine`` and reads ``engine.spoke_push`` (or any
other name below) keeps the surface it had.
"""
from __future__ import annotations

from apps.sync_hub.engine_apply import (
    ApplyResult,
    finalize_identity_repairs,
    hub_apply,
    retire_tombstoned_remaps,
    spoke_apply,
)
from apps.sync_hub.engine_changes import (
    ChangeBatch,
    HeldRow,
    Offer,
    hub_changes_since,
    hub_track_bundles,
    identity_repair_offer,
    relog_held,
    spoke_push,
    still_held_rows,
)
from apps.sync_hub.engine_common import (
    CHANGELOG_TABLES,
    DEFAULT_KEEP_DAYS,
    DEFAULT_KEEP_ROWS,
    DEFAULT_PULL_LIMIT,
    HUB_CHANGELOG_TABLE,
    SyncApplyError,
    SyncSchemaMismatch,
)
from apps.sync_hub.engine_identity import (
    SyncIdentityPreflightError,
    assert_identity_ready,
    hub_library_size,
)
from apps.sync_hub.engine_machines import machines_snapshot, merge_machines, upsert_machine
from apps.sync_hub.engine_retention import prune_changelog
from apps.sync_hub.engine_watermark import (
    Watermark,
    current_seq,
    local_seq,
    read_watermark,
    settled_push_seq,
    write_watermark,
)

__all__ = [
    "CHANGELOG_TABLES",
    "DEFAULT_KEEP_DAYS",
    "DEFAULT_KEEP_ROWS",
    "DEFAULT_PULL_LIMIT",
    "HUB_CHANGELOG_TABLE",
    "ApplyResult",
    "ChangeBatch",
    "HeldRow",
    "Offer",
    "SyncApplyError",
    "SyncIdentityPreflightError",
    "SyncSchemaMismatch",
    "Watermark",
    "assert_identity_ready",
    "current_seq",
    "finalize_identity_repairs",
    "hub_apply",
    "hub_changes_since",
    "hub_library_size",
    "hub_track_bundles",
    "identity_repair_offer",
    "local_seq",
    "machines_snapshot",
    "merge_machines",
    "prune_changelog",
    "read_watermark",
    "relog_held",
    "retire_tombstoned_remaps",
    "settled_push_seq",
    "spoke_apply",
    "spoke_push",
    "still_held_rows",
    "upsert_machine",
    "write_watermark",
]
