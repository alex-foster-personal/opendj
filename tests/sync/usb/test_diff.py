"""Tests for apps.sync.usb.diff."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apps.sync.usb.diff import (
    compute_plan,
    filter_plan_by_files,
    filter_plan_by_playlists,
    plan_summary,
)


@pytest.mark.requirement("CAT-02")
def test_first_run_all_copies(fixture_profile, fixture_canonical, drive_root) -> None:
    plan = compute_plan(
        profile=fixture_profile,
        canonical=fixture_canonical,
        drive_root=drive_root,
    )
    counts = plan_summary(plan)
    assert counts.get("copy", 0) == 3
    assert counts.get("delete", 0) == 0
    assert plan.total_bytes > 0
    assert plan.free_bytes_needed >= plan.total_bytes


@pytest.mark.requirement("CAT-02")
def test_idempotent_second_run(
    fixture_profile, fixture_canonical, drive_root
) -> None:
    # Copy each source to its expected destination (simulate a prior apply).
    plan = compute_plan(
        profile=fixture_profile,
        canonical=fixture_canonical,
        drive_root=drive_root,
    )
    for op in plan.ops:
        assert op.src is not None
        op.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(op.src, op.dst)

    plan2 = compute_plan(
        profile=fixture_profile,
        canonical=fixture_canonical,
        drive_root=drive_root,
    )
    counts = plan_summary(plan2)
    # Everything is current; no ops.
    assert counts.get("copy", 0) == 0
    assert counts.get("overwrite", 0) == 0
    assert counts.get("delete", 0) == 0
    assert plan2.existing_bytes > 0


@pytest.mark.requirement("CAT-02")
def test_orphan_delete(fixture_profile, fixture_canonical, drive_root) -> None:
    # Put a file on the drive that the profile does not mention.
    orphan = drive_root / "Other/Thing.mp3"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    plan = compute_plan(
        profile=fixture_profile,
        canonical=fixture_canonical,
        drive_root=drive_root,
    )
    delete_ops = [op for op in plan.ops if op.kind == "delete"]
    assert len(delete_ops) == 1
    assert delete_ops[0].dst == orphan


@pytest.mark.requirement("CAT-02")
def test_drift_emits_overwrite(
    fixture_profile, fixture_canonical, drive_root
) -> None:
    # Place a differently-sized file at one expected destination.
    plan0 = compute_plan(
        profile=fixture_profile,
        canonical=fixture_canonical,
        drive_root=drive_root,
    )
    first = plan0.ops[0]
    first.dst.parent.mkdir(parents=True, exist_ok=True)
    first.dst.write_bytes(b"garbage-different-size")
    plan = compute_plan(
        profile=fixture_profile,
        canonical=fixture_canonical,
        drive_root=drive_root,
    )
    kinds = [op.kind for op in plan.ops if op.dst == first.dst]
    assert kinds == ["overwrite"]


@pytest.mark.requirement("CAT-02")
def test_skip_policy_emits_skip(
    fixture_canonical, drive_root
) -> None:
    from apps.sync.usb.profile import load_from_string

    profile = load_from_string(
        """
name: fixtureA
drive_label: FIXTURE-A
playlists:
  - Warmup
  - Peak
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: skip
"""
    )
    # Put a drifted file at one expected dst.
    plan0 = compute_plan(
        profile=profile,
        canonical=fixture_canonical,
        drive_root=drive_root,
    )
    first = plan0.ops[0]
    first.dst.parent.mkdir(parents=True, exist_ok=True)
    first.dst.write_bytes(b"diff")
    plan = compute_plan(
        profile=profile,
        canonical=fixture_canonical,
        drive_root=drive_root,
    )
    kinds = [op.kind for op in plan.ops if op.dst == first.dst]
    assert kinds == ["skip"]


@pytest.mark.requirement("CAT-02")
def test_filter_by_playlists(
    fixture_profile, fixture_canonical, drive_root
) -> None:
    plan = compute_plan(
        profile=fixture_profile,
        canonical=fixture_canonical,
        drive_root=drive_root,
    )
    sub = filter_plan_by_playlists(plan, ["Peak"])
    # Peak contains one track.
    copy_ops = [op for op in sub.ops if op.kind == "copy"]
    assert len(copy_ops) == 1


@pytest.mark.requirement("CAT-02")
def test_filter_by_files(
    fixture_profile, fixture_canonical, drive_root
) -> None:
    plan = compute_plan(
        profile=fixture_profile,
        canonical=fixture_canonical,
        drive_root=drive_root,
    )
    target = str(plan.ops[0].dst_rel)
    sub = filter_plan_by_files(plan, [target])
    assert len(sub.ops) == 1
    assert str(sub.ops[0].dst_rel) == target


@pytest.mark.requirement("CAT-02")
def test_exclusions_preserve_drive_files(
    fixture_canonical, drive_root
) -> None:
    from apps.sync.usb.profile import load_from_string

    prof = load_from_string(
        """
name: fx
drive_label: FIXTURE-A
playlists: [Warmup, Peak]
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: none
conflict_policy: canonical-wins
exclusions:
  - samples/
"""
    )
    protected = drive_root / "samples" / "keep.mp3"
    protected.parent.mkdir()
    protected.write_bytes(b"keep")
    plan = compute_plan(
        profile=prof,
        canonical=fixture_canonical,
        drive_root=drive_root,
    )
    deletes = [op.dst for op in plan.ops if op.kind == "delete"]
    assert protected not in deletes


@pytest.mark.requirement("CAT-02")
def test_skip_patterns_not_deleted(
    fixture_profile, fixture_canonical, drive_root
) -> None:
    # Drop a marker + Playlists file which should never be candidates.
    (drive_root / ".mdj-marker.json").write_text("{}")
    (drive_root / "Playlists").mkdir()
    (drive_root / "Playlists" / "Warmup.m3u8").write_text("#EXTM3U\n")
    plan = compute_plan(
        profile=fixture_profile,
        canonical=fixture_canonical,
        drive_root=drive_root,
    )
    deletes = [str(op.dst_rel) for op in plan.ops if op.kind == "delete"]
    assert ".mdj-marker.json" not in deletes
    assert not any(d.startswith("Playlists/") for d in deletes)
