"""Tests for apps.sync.usb.preflight."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync.usb.diff import Op, Plan, compute_plan
from apps.sync.usb.preflight import ALL_CHECKS, preflight


def _small_plan(drive_root: Path) -> Plan:
    return Plan(
        profile_name="fixtureA",
        drive_root=drive_root,
        ops=[],
        total_bytes=0,
        existing_bytes=0,
        free_bytes_needed=0,
    )


@pytest.mark.requirement("CAT-02")
def test_drive_mounted_error(fixture_profile, tmp_path) -> None:
    missing = tmp_path / "missing"
    res = preflight(
        fixture_profile, _small_plan(missing), drive_root=missing,
    )
    assert not res.ok
    assert any("drive_mounted" in e for e in res.errors)


@pytest.mark.requirement("CAT-02")
def test_drive_label_mismatch(fixture_profile, tmp_path) -> None:
    bad = tmp_path / "WRONG-LABEL"
    bad.mkdir()
    res = preflight(fixture_profile, _small_plan(bad), drive_root=bad)
    assert any("drive_label" in e for e in res.errors)


@pytest.mark.requirement("CAT-02")
def test_free_space_error(fixture_profile, drive_root) -> None:
    plan = Plan(
        profile_name="x",
        drive_root=drive_root,
        ops=[],
        total_bytes=10_000_000_000,
        existing_bytes=0,
        free_bytes_needed=10_000_000_000,
    )
    res = preflight(
        fixture_profile,
        plan,
        drive_root=drive_root,
        drive_free_bytes=100,  # clearly not enough
    )
    assert not res.ok
    assert any("free_space" in e for e in res.errors)


@pytest.mark.requirement("CAT-02")
def test_write_probe_success(fixture_profile, drive_root) -> None:
    res = preflight(
        fixture_profile,
        _small_plan(drive_root),
        drive_root=drive_root,
        write_probe=True,
        drive_free_bytes=10_000_000_000,
    )
    # The drive is writable + the label matches -> ok.
    assert res.ok


@pytest.mark.requirement("CAT-02")
def test_skip_check_suppresses(fixture_profile, tmp_path) -> None:
    bad = tmp_path / "WRONG-LABEL"
    bad.mkdir()
    res = preflight(
        fixture_profile,
        _small_plan(bad),
        drive_root=bad,
        skip_checks={"drive_label"},
        drive_free_bytes=10_000_000_000,
    )
    assert "drive_label" in res.skipped
    assert not any("drive_label" in e for e in res.errors)


@pytest.mark.requirement("CAT-02")
def test_ffmpeg_missing_triggers_error(fixture_canonical, drive_root, monkeypatch) -> None:
    from apps.sync.usb.profile import load_from_string
    profile = load_from_string(
        """
name: fixtureA
drive_label: FIXTURE-A
playlists: [Warmup, Peak]
format: mp3@320
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: canonical-wins
"""
    )
    plan = compute_plan(
        profile=profile, canonical=fixture_canonical, drive_root=drive_root
    )
    # Force which() to return None so the check fails.
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _x: None)
    res = preflight(
        profile,
        plan,
        drive_root=drive_root,
        drive_free_bytes=10_000_000_000,
    )
    assert any("ffmpeg_available" in e for e in res.errors)


@pytest.mark.requirement("CAT-02")
def test_case_collision_detected(drive_root) -> None:
    from pathlib import PurePosixPath
    ops = [
        Op(
            kind="copy",
            dst=drive_root / "A/x.mp3",
            dst_rel=PurePosixPath("A/x.mp3"),
            src=Path("/src/1"),
            stable_id="a",
            expected_hash="h",
            reason="r",
            bytes_estimate=1,
        ),
        Op(
            kind="copy",
            dst=drive_root / "a/x.mp3",
            dst_rel=PurePosixPath("a/x.mp3"),
            src=Path("/src/2"),
            stable_id="b",
            expected_hash="h",
            reason="r",
            bytes_estimate=1,
        ),
    ]
    from apps.sync.usb.profile import load_from_string
    profile = load_from_string(
        """
name: x
drive_label: FIXTURE-A
playlists: [Warmup, Peak]
format: copy-as-is
layout: flat
playlist_files: none
conflict_policy: canonical-wins
"""
    )
    plan = Plan(
        profile_name="x",
        drive_root=drive_root,
        ops=ops,
        total_bytes=2,
        existing_bytes=0,
        free_bytes_needed=2,
    )
    res = preflight(
        profile, plan, drive_root=drive_root, drive_free_bytes=10_000_000_000
    )
    assert any("no_case_collisions" in e for e in res.errors)


@pytest.mark.requirement("CAT-02")
def test_all_checks_enumerated() -> None:
    for check in ALL_CHECKS:
        assert isinstance(check, str) and check
