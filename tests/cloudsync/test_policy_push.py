"""Unit tests for :mod:`apps.sync_hub.policy_push` row mapping."""
from __future__ import annotations

from apps.sync_hub import protocol
from apps.sync_hub.policy_push import proposed_from_sync_changes

_STAMP = "2026-09-15T00:00:00.000000+00:00"
_AUTHOR = "author-machine"


def test_proposed_from_sync_changes_returns_none_for_non_policy_tables() -> None:
    """if non-policy rows map to a proposal then push validation runs too often."""
    change = protocol.RowChange(
        table="tracks",
        pk=("trk-1",),
        values={
            "stable_id": "trk-1",
            "title": "x",
            "updated_at": _STAMP,
            "origin_device_id": _AUTHOR,
            "deleted_at": None,
        },
    )
    assert proposed_from_sync_changes(_AUTHOR, [change]) is None


def test_proposed_from_sync_changes_maps_live_policy_cell() -> None:
    """if a live sync_policies row is not mapped then the gate never sees it."""
    change = protocol.RowChange(
        table="sync_policies",
        pk=(_AUTHOR, "audio"),
        values={
            "machine_id": _AUTHOR,
            "asset_kind": "audio",
            "mode": "cached",
            "cache_budget_mb": 128,
            "updated_at": _STAMP,
            "origin_device_id": _AUTHOR,
            "deleted_at": None,
        },
    )
    proposed = proposed_from_sync_changes(_AUTHOR, [change])
    assert proposed is not None
    assert len(proposed.policies) == 1
    cell = proposed.policies[0]
    assert cell.machine_id == _AUTHOR
    assert cell.asset_kind == "audio"
    assert cell.mode == "cached"
    assert cell.cache_budget_mb == 128
    assert proposed.removed_policies == ()


def test_proposed_from_sync_changes_maps_policy_tombstone() -> None:
    """if a tombstoned sync_policies row is not a removal then unset is judged wrong."""
    change = protocol.RowChange(
        table="sync_policies",
        pk=(_AUTHOR, "audio"),
        values={
            "machine_id": _AUTHOR,
            "asset_kind": "audio",
            "mode": "cached",
            "cache_budget_mb": None,
            "updated_at": _STAMP,
            "origin_device_id": _AUTHOR,
            "deleted_at": _STAMP,
        },
    )
    proposed = proposed_from_sync_changes(_AUTHOR, [change])
    assert proposed is not None
    assert proposed.policies == ()
    assert len(proposed.removed_policies) == 1
    key = proposed.removed_policies[0]
    assert key.machine_id == _AUTHOR
    assert key.asset_kind == "audio"
