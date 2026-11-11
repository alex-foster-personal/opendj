"""Parallel playlist copy across fake sticks, joined by one fail-closed receipt.

A pure planner splits one playlist across N stick directories, the apply seam runs
one copy job per stick on its own thread, and the verify seam re-hashes every
planned file per stick. The sticks are temporary directories, so nothing here
needs a real USB device.

[if] any stick fails to copy or verify [then] the joined receipt is not OK, [else stop].
"""

from __future__ import annotations

import dataclasses
import os
import time
from collections import Counter
from pathlib import Path

import pytest

from apps.sync.usb.apply import apply_parallel_copy
from apps.sync.usb.copy import copy_one
from apps.sync.usb.plan import plan_parallel_playlist_copy
from apps.sync.usb.verify import verify_parallel_copy

pytestmark = pytest.mark.requirement("USBEXP-12648")

PLAYLIST = "Warmup"
LAYOUT = "Artist/Album/Track"

# chmod-based directory permissions are POSIX-only: on Windows chmod does not
# remove directory write or traverse access, and root ignores them entirely.
posix_dir_permissions = pytest.mark.skipif(
    os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX directory permissions as a non-root user (Windows chmod and root ignore them)",
)


def _sticks(tmp_path: Path, names=("STICK-A", "STICK-B", "STICK-C")) -> dict[str, Path]:
    out = {}
    for name in names:
        root = tmp_path / "volumes" / name
        root.mkdir(parents=True)
        out[name] = root
    return out


def _tracks(make_track, n: int = 7):
    tracks = [
        make_track(title=f"T{i}", artist=f"A{i}", album="LP", playlist=PLAYLIST, payload=os.urandom(64 + 32 * i))
        for i in range(n)
    ]
    # A track from another playlist is never planned.
    tracks.append(make_track(title="Other", artist="Z", album="LP", playlist="Peak"))
    return tracks


def _plan(tmp_path, make_track, names=("STICK-A", "STICK-B", "STICK-C")):
    tracks = _tracks(make_track)
    sticks = _sticks(tmp_path, names)
    return tracks, plan_parallel_playlist_copy(playlist=PLAYLIST, tracks=tracks, sticks=sticks, layout=LAYOUT)


def test_each_track_is_assigned_once_and_every_stick_is_named(tmp_path, make_track):
    """One playlist planned for three writable fake sticks.

    [if] one playlist is planned for three sticks [then] each track is assigned once and every stick is named, [else stop].
    """
    tracks, plan = _plan(tmp_path, make_track)
    selected = [t.stable_id for t in tracks if t.playlist == PLAYLIST]

    planned = Counter(op.stable_id for job in plan.jobs for op in job.ops)
    assert sorted(planned) == sorted(selected)
    assert set(planned.values()) == {1}
    assert plan.sticks == ("STICK-A", "STICK-B", "STICK-C")
    assert all(job.ops for job in plan.jobs), "seven tracks over three sticks leaves no stick empty"
    for job in plan.jobs:
        assert all(op.dst.is_relative_to(job.root) for op in job.ops)
    # Planning is pure: nothing was written to any stick.
    assert not any(any(root.iterdir()) for root in (job.root for job in plan.jobs))


def test_planner_names_an_empty_stick_and_refuses_transcodes(tmp_path, make_track):
    """[if] there are fewer tracks than sticks or a transcode format [then] every stick is still named and a transcode is refused, [else stop]."""
    one = [make_track(title="Solo", playlist=PLAYLIST)]
    sticks = _sticks(tmp_path)
    plan = plan_parallel_playlist_copy(playlist=PLAYLIST, tracks=one, sticks=sticks, layout=LAYOUT)
    assert plan.sticks == tuple(sticks)
    assert sum(len(job.ops) for job in plan.jobs) == 1
    with pytest.raises(ValueError):
        plan_parallel_playlist_copy(playlist=PLAYLIST, tracks=one, sticks=sticks, layout=LAYOUT, format_="mp3@320")
    with pytest.raises(ValueError):
        plan_parallel_playlist_copy(playlist=PLAYLIST, tracks=one, sticks={}, layout=LAYOUT)


def test_unequal_jobs_run_in_parallel(tmp_path, make_track, fixture_profile):
    """[if] three copy jobs have controlled unequal delays [then] joined wall time stays within 1.3x the slowest job, [else stop]."""
    _, plan = _plan(tmp_path, make_track)
    delay = {"STICK-A": 0.15, "STICK-B": 0.3, "STICK-C": 0.6}
    # Each stick's delay is spread over its own ops, so its job takes about that long.
    pause = {op.dst: delay[job.stick] / len(job.ops) for job in plan.jobs for op in job.ops}

    def slow_copy(op, profile):
        time.sleep(pause[op.dst])
        return copy_one(op, profile)

    applied = apply_parallel_copy(plan, fixture_profile, copy=slow_copy)
    slowest = max(r.seconds for r in applied.sticks.values())

    assert applied.ok
    assert slowest >= 0.6
    assert applied.wall_seconds <= 1.3 * slowest
    assert verify_parallel_copy(plan, applied).ok


@pytest.mark.parametrize("damage", ["missing", "mismatch"])
def test_bad_stick_is_never_marked_ok(tmp_path, make_track, fixture_profile, damage):
    """[if] one fake stick verification reports a missing or mismatched file [then] the joined report does not mark that stick OK, [else stop]."""
    _, plan = _plan(tmp_path, make_track)
    applied = apply_parallel_copy(plan, fixture_profile)
    assert verify_parallel_copy(plan, applied).ok

    victim = plan.jobs[1].ops[0].dst
    if damage == "missing":
        victim.unlink()
    else:
        victim.write_bytes(b"not the library bytes")

    receipt = verify_parallel_copy(plan, applied)
    assert not receipt.sticks["STICK-B"].ok
    assert any(p.startswith(damage) for p in receipt.sticks["STICK-B"].problems)
    assert receipt.sticks["STICK-A"].ok and receipt.sticks["STICK-C"].ok
    assert not receipt.ok


def test_unwritable_target_never_starts_a_job(tmp_path, make_track, fixture_profile):
    """[if] a target is unwritable before apply [then] no copy job starts for that target and the receipt is not OK, [else stop]."""
    _, plan = _plan(tmp_path, make_track)
    dead = plan.jobs[2].root
    dead.rmdir()
    dead.write_bytes(b"")  # the mount path is now a file: nothing can be written under it

    touched: list[Path] = []

    def recording_copy(op, profile):
        touched.append(op.dst)
        return copy_one(op, profile)

    applied = apply_parallel_copy(plan, fixture_profile, copy=recording_copy)

    assert applied.sticks["STICK-C"].started is False
    assert applied.sticks["STICK-C"].refused
    assert not any(p.is_relative_to(dead) for p in touched)
    assert applied.sticks["STICK-A"].ok and applied.sticks["STICK-B"].ok
    receipt = verify_parallel_copy(plan, applied)
    assert not receipt.sticks["STICK-C"].ok
    assert not receipt.ok


@posix_dir_permissions
def test_permission_denied_target_is_refused(tmp_path, make_track, fixture_profile):
    """[if] a stick directory is read-only [then] its job is refused before any copy, [else stop]."""
    _, plan = _plan(tmp_path, make_track)
    ro = plan.jobs[0].root
    ro.chmod(0o555)
    try:
        applied = apply_parallel_copy(plan, fixture_profile)
    finally:
        ro.chmod(0o755)
    assert applied.sticks["STICK-A"].started is False
    assert not any(ro.iterdir())


def _colliding_pair(make_track):
    """Two tracks whose destinations differ only by case, from distinct source files.

    The sources get unique names first (so a case-insensitive host filesystem
    cannot alias them), then the metadata is changed to make the collision.
    """
    a = make_track(title="IntroA", artist="DJ", album="LP-a", playlist=PLAYLIST, payload=os.urandom(256))
    b = make_track(title="IntroB", artist="DJ", album="LP-b", playlist=PLAYLIST, payload=os.urandom(128))
    return (
        dataclasses.replace(a, title="Intro", album="LP"),
        dataclasses.replace(b, title="Intro", album="lp"),
    )


def test_colliding_destinations_never_share_a_stick(tmp_path, make_track, fixture_profile):
    """[if] two tracks map to the same path (case-folded) [then] they never land on the same stick, [else stop]."""
    a, b = _colliding_pair(make_track)
    sticks = _sticks(tmp_path, names=("STICK-A", "STICK-B"))
    plan = plan_parallel_playlist_copy(playlist=PLAYLIST, tracks=[a, b], sticks=sticks, layout=LAYOUT)

    for job in plan.jobs:
        keys = [op.dst_rel.as_posix().casefold() for op in job.ops]
        assert len(keys) == len(set(keys))
    assert set(plan.assignments()) == {a.stable_id, b.stable_id}
    assert not plan.warnings
    applied = apply_parallel_copy(plan, fixture_profile)
    assert verify_parallel_copy(plan, applied).ok


def test_collision_taken_on_every_stick_is_left_out_with_a_warning(tmp_path, make_track, fixture_profile):
    """[if] a destination is taken on every stick [then] the extra track is left out, warned about, and fails the receipt, [else stop]."""
    a, b = _colliding_pair(make_track)
    sticks = _sticks(tmp_path, names=("STICK-A",))
    plan = plan_parallel_playlist_copy(playlist=PLAYLIST, tracks=[a, b], sticks=sticks, layout=LAYOUT)

    assert list(plan.assignments()) == [a.stable_id]
    assert len(plan.warnings) == 1 and b.stable_id in plan.warnings[0]
    assert plan.omitted == (b.stable_id,)
    applied = apply_parallel_copy(plan, fixture_profile)
    assert applied.ok
    receipt = verify_parallel_copy(plan, applied)
    assert receipt.sticks["STICK-A"].ok
    assert receipt.omitted == (b.stable_id,)
    assert not receipt.ok, "a playlist copy that left a track out is not a successful copy"


def test_skip_policy_never_overwrites_an_existing_file(tmp_path, make_track, fixture_profile):
    """[if] conflict_policy is skip and a destination already exists [then] it is not overwritten and that stick is not OK, [else stop]."""
    _, plan = _plan(tmp_path, make_track)
    skip_profile = dataclasses.replace(fixture_profile, conflict_policy="skip")
    edited = plan.jobs[0].ops[0]
    edited.dst.parent.mkdir(parents=True, exist_ok=True)
    edited.dst.write_bytes(b"on-stick edit")
    same = plan.jobs[1].ops[0]
    same.dst.parent.mkdir(parents=True, exist_ok=True)
    same.dst.write_bytes(same.src.read_bytes())

    applied = apply_parallel_copy(plan, skip_profile)

    assert edited.dst.read_bytes() == b"on-stick edit"
    assert not applied.sticks["STICK-A"].ok
    assert any("conflict_policy=skip" in (r.error or "") for r in applied.sticks["STICK-A"].results)
    assert applied.sticks["STICK-B"].ok and applied.sticks["STICK-C"].ok
    receipt = verify_parallel_copy(plan, applied)
    assert not receipt.sticks["STICK-A"].ok and not receipt.ok


@posix_dir_permissions
def test_untraversable_destination_is_recorded_not_raised(tmp_path, make_track, fixture_profile):
    """[if] a destination's parent cannot be traversed [then] verify records it unreadable and still joins the other sticks, [else stop]."""
    _, plan = _plan(tmp_path, make_track)
    applied = apply_parallel_copy(plan, fixture_profile)
    locked = plan.jobs[1].ops[0].dst.parent
    locked.chmod(0o000)
    try:
        receipt = verify_parallel_copy(plan, applied)
    finally:
        locked.chmod(0o755)
    assert not receipt.sticks["STICK-B"].ok
    assert any(p.startswith("unreadable") for p in receipt.sticks["STICK-B"].problems)
    assert receipt.sticks["STICK-A"].ok and receipt.sticks["STICK-C"].ok


@posix_dir_permissions
def test_uninspectable_target_is_refused_not_raised(tmp_path, make_track, fixture_profile):
    """[if] a target sits under a directory that cannot be traversed [then] that stick is refused and the others still copy, [else stop]."""
    sticks = _sticks(tmp_path, names=("STICK-A", "STICK-B"))
    gate = tmp_path / "locked"
    hidden = gate / "STICK-C"
    hidden.mkdir(parents=True)
    sticks["STICK-C"] = hidden
    plan = plan_parallel_playlist_copy(playlist=PLAYLIST, tracks=_tracks(make_track), sticks=sticks, layout=LAYOUT)
    gate.chmod(0o000)
    try:
        applied = apply_parallel_copy(plan, fixture_profile)
    finally:
        gate.chmod(0o755)
    assert applied.sticks["STICK-C"].started is False
    assert applied.sticks["STICK-C"].refused
    assert applied.sticks["STICK-A"].ok and applied.sticks["STICK-B"].ok
    assert not verify_parallel_copy(plan, applied).ok
