"""SYNC-03: plan builder (pure, no DB I/O).

Exercises the RB-canonical bi-directional diff against matcher output:

* CONTEXT D1 (RB canonical).
* CONTEXT D2 (only matched tracks written; unmatched reported).
* CONTEXT D3 (djay-only playlists reported, never touched).
* CONTEXT D6 (flatten folder hierarchy with " / " separator).
"""
from __future__ import annotations

import pytest

from apps.sync.playlist_plan import (
    DjayPlaylistRead,
    FlatPlaylist,
    MatchSet,
    build_plan,
)


def _flat(
    rb_id: str,
    name: str,
    track_ids: tuple[str, ...],
    parent: tuple[str, ...] = (),
) -> FlatPlaylist:
    return FlatPlaylist(
        rb_id=rb_id,
        flat_name=name,
        parent_path=parent,
        track_ids_ordered=track_ids,
    )


def _matches(pairs: dict[str, str]) -> MatchSet:
    return MatchSet(
        rb_to_djay=dict(pairs),
        djay_to_rb={v: k for k, v in pairs.items()},
        source_sha256="fake-sha",
        source_path="fake/matches.csv",
    )


@pytest.mark.requirement("SYNC-03")
def test_plan_creates_new_djay_playlist_when_no_collision() -> None:
    rb = [_flat("r1", "Warmup", ("t1", "t2"))]
    matches = _matches({"t1": "u1", "t2": "u2"})
    plan = build_plan(rb, [], matches)
    assert len(plan.playlists) == 1
    op = plan.playlists[0]
    assert op.op == "create"
    assert op.djay_uuid is None
    assert [m.djay_uuid for m in op.target_members] == ["u1", "u2"]
    assert [m.track_no for m in op.target_members] == [1, 2]
    assert op.adds == op.target_members
    assert not op.removes
    assert not op.unmatched_rb
    assert plan.djay_only == []


@pytest.mark.requirement("SYNC-03")
def test_plan_updates_colliding_djay_playlist_with_adds_and_removes() -> None:
    rb = [_flat("r1", "Warmup", ("t1", "t2", "t3"))]
    djay = [
        DjayPlaylistRead(
            uuid="pl1",
            name="warmup",  # case difference
            member_uuids_ordered=("u2", "u_legacy"),
        )
    ]
    matches = _matches({"t1": "u1", "t2": "u2", "t3": "u3"})
    plan = build_plan(rb, djay, matches)
    op = plan.playlists[0]
    assert op.op == "update"
    assert op.djay_uuid == "pl1"
    assert [m.djay_uuid for m in op.adds] == ["u1", "u3"]
    assert op.removes == ["u_legacy"]
    assert plan.djay_only == []


@pytest.mark.requirement("SYNC-03")
def test_plan_reports_djay_only_playlists_as_noop() -> None:
    rb = []
    djay = [
        DjayPlaylistRead(uuid="pl_djay", name="User's Private", member_uuids_ordered=("u1",))
    ]
    plan = build_plan(rb, djay, _matches({}))
    assert plan.playlists == []
    assert len(plan.djay_only) == 1
    assert plan.djay_only[0].djay_uuid == "pl_djay"
    assert plan.djay_only[0].member_count == 1


@pytest.mark.requirement("SYNC-03")
def test_plan_reports_unmatched_rb_tracks_per_playlist() -> None:
    rb = [_flat("r1", "Set", ("t1", "t_missing"))]
    matches = _matches({"t1": "u1"})  # t_missing has no confirmed match
    plan = build_plan(
        rb,
        [],
        matches,
        rb_track_titles={"t_missing": ("Ghost Title", "Ghost Artist")},
    )
    op = plan.playlists[0]
    assert len(op.unmatched_rb) == 1
    assert op.unmatched_rb[0].rb_id == "t_missing"
    assert op.unmatched_rb[0].title == "Ghost Title"
    # Target + adds only contain the matched track.
    assert [m.rb_id for m in op.target_members] == ["t1"]
    assert op.op == "create"


@pytest.mark.requirement("SYNC-03")
def test_plan_noop_when_membership_already_matches() -> None:
    rb = [_flat("r1", "Chill", ("t1", "t2"))]
    djay = [
        DjayPlaylistRead(uuid="pl1", name="Chill", member_uuids_ordered=("u1", "u2"))
    ]
    plan = build_plan(rb, djay, _matches({"t1": "u1", "t2": "u2"}))
    op = plan.playlists[0]
    assert op.op == "noop"
    assert not op.adds
    assert not op.removes
    assert not op.reordered


@pytest.mark.requirement("SYNC-03")
def test_plan_flags_reorder_when_only_order_differs() -> None:
    rb = [_flat("r1", "Flow", ("t1", "t2", "t3"))]
    djay = [
        DjayPlaylistRead(uuid="pl1", name="Flow", member_uuids_ordered=("u2", "u1", "u3"))
    ]
    plan = build_plan(rb, djay, _matches({"t1": "u1", "t2": "u2", "t3": "u3"}))
    op = plan.playlists[0]
    assert op.op == "update"
    assert not op.adds
    assert not op.removes
    assert op.reordered


@pytest.mark.requirement("SYNC-03")
def test_plan_name_collation_is_nfc_casefold() -> None:
    rb = [_flat("r1", "CAFÉ", ("t1",))]  # NFC-composed é, upper-case
    djay = [
        DjayPlaylistRead(
            uuid="pl1",
            name="café",  # lower-case, NFC too (same composed é)
            member_uuids_ordered=("u1",),
        )
    ]
    plan = build_plan(rb, djay, _matches({"t1": "u1"}))
    assert plan.playlists[0].op == "noop"
    # RB casing is preserved on output.
    assert plan.playlists[0].rb_name == "CAFÉ"


@pytest.mark.requirement("SYNC-03")
def test_plan_preserves_generated_at_and_sha_provenance() -> None:
    plan = build_plan([], [], _matches({"t1": "u1"}), generated_at="2026-04-17T00:00:00Z")
    assert plan.generated_at == "2026-04-17T00:00:00Z"
    assert plan.match_set_sha256 == "fake-sha"


@pytest.mark.requirement("SYNC-03")
def test_plan_djay_only_sorted_deterministically() -> None:
    djay = [
        DjayPlaylistRead(uuid="b", name="Zeta", member_uuids_ordered=()),
        DjayPlaylistRead(uuid="a", name="Alpha", member_uuids_ordered=("u1",)),
    ]
    plan = build_plan([], djay, _matches({}))
    assert [d.djay_uuid for d in plan.djay_only] == ["a", "b"]


@pytest.mark.requirement("SYNC-03")
def test_plan_mixed_adds_removes_and_unmatched_coexist() -> None:
    rb = [
        _flat("r1", "Big Room", ("t1", "t2", "t_miss")),
    ]
    djay = [
        DjayPlaylistRead(
            uuid="pl1",
            name="Big Room",
            member_uuids_ordered=("u2", "u_old"),
        )
    ]
    matches = _matches({"t1": "u1", "t2": "u2"})
    plan = build_plan(rb, djay, matches)
    op = plan.playlists[0]
    assert op.op == "update"
    assert [a.rb_id for a in op.adds] == ["t1"]
    assert op.removes == ["u_old"]
    assert [u.rb_id for u in op.unmatched_rb] == ["t_miss"]
