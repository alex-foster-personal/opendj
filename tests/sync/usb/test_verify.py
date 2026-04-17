"""Tests for apps.sync.usb.verify engine + CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.sync.usb import apply as apply_mod
from apps.sync.usb import verify as verify_mod
from apps.sync.usb.profile import load_from_string
from apps.sync.usb.state import CanonicalTrack
from apps.sync.usb.verify import FileStatus, plan_from_verify, verify_drive


def _profile():
    return load_from_string(
        """
name: fixtureA
drive_label: FIXTURE-A
playlists: [Warmup, Peak]
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: canonical-wins
"""
    )


def _apply(fixture_canonical, drive_root) -> None:
    """Synthesise a drive that matches canonical by copying bytes directly."""
    from apps.sync.usb.diff import compute_plan
    profile = _profile()
    plan = compute_plan(
        profile=profile, canonical=fixture_canonical, drive_root=drive_root
    )
    import shutil
    for op in plan.ops:
        assert op.src is not None
        op.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(op.src, op.dst)


@pytest.mark.requirement("CAT-02")
def test_verify_all_ok(fixture_canonical, drive_root) -> None:
    _apply(fixture_canonical, drive_root)
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert report.ok == 3
    assert report.missing == 0
    assert report.extra == 0
    assert report.corrupted == 0
    assert report.renamed == 0


@pytest.mark.requirement("CAT-02")
def test_verify_missing(fixture_canonical, drive_root) -> None:
    _apply(fixture_canonical, drive_root)
    # Delete one.
    (drive_root / "Alice" / "AA" / "One.mp3").unlink()
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert report.missing == 1
    assert report.ok == 2


@pytest.mark.requirement("CAT-02")
def test_verify_corrupted(fixture_canonical, drive_root) -> None:
    _apply(fixture_canonical, drive_root)
    # Scramble one.
    (drive_root / "Alice" / "AA" / "One.mp3").write_bytes(b"corrupt")
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert report.corrupted == 1


@pytest.mark.requirement("CAT-02")
def test_verify_extra_without_rename(fixture_canonical, drive_root) -> None:
    _apply(fixture_canonical, drive_root)
    extra = drive_root / "Random" / "Thing.mp3"
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b"random unrelated bytes")
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert report.extra == 1
    assert report.renamed == 0


@pytest.mark.requirement("CAT-02")
def test_verify_renamed(fixture_canonical, drive_root) -> None:
    _apply(fixture_canonical, drive_root)
    # Move a file from its expected path to another path; keep the bytes.
    src = drive_root / "Alice" / "AA" / "One.mp3"
    dst = drive_root / "Relocated" / "One.mp3"
    dst.parent.mkdir(parents=True)
    src.rename(dst)
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    # One renamed, one missing for the expected path is reported as RENAMED,
    # not MISSING + EXTRA.
    assert report.renamed == 1
    # The expected path is absent -> still MISSING. But EXTRA 0.
    assert report.missing == 1
    assert report.extra == 0


@pytest.mark.requirement("CAT-02")
def test_verify_playlist_broken(fixture_canonical, drive_root) -> None:
    _apply(fixture_canonical, drive_root)
    pl = drive_root / "Playlists"
    pl.mkdir()
    (pl / "Warmup.m3u8").write_text(
        "#EXTM3U\n#EXTINF:120,Alice - One\n../Alice/AA/One.mp3\n../Does/Not/Exist.mp3\n",
        encoding="utf-8",
    )
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert "Warmup.m3u8" in report.playlists_broken


@pytest.mark.requirement("CAT-02")
def test_verify_cli_exit_codes(
    fixture_canonical, drive_root, tmp_path, monkeypatch
) -> None:
    _apply(fixture_canonical, drive_root)

    def fake_loader(*, playlist_names, use_shared_state=False, hash_cache=None, db=None):
        return fixture_canonical

    monkeypatch.setattr(
        "apps.sync.usb.verify.load_canonical_tracks", fake_loader
    )

    pf = tmp_path / "profile.yaml"
    pf.write_text(
        """
name: fixtureA
drive_label: FIXTURE-A
playlists: [Warmup, Peak]
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: canonical-wins
""",
        encoding="utf-8",
    )
    # Happy path -> exit 0.
    out_json = tmp_path / "verify.json"
    rc = verify_mod.main(
        [
            "--profile",
            str(pf),
            "--drive-root",
            str(drive_root),
            "--json",
            str(out_json),
        ]
    )
    assert rc == 0
    data = json.loads(out_json.read_text())
    assert data["counts"]["ok"] == 3
    # Delete a file -> exit 5.
    (drive_root / "Alice" / "AA" / "One.mp3").unlink()
    rc2 = verify_mod.main(
        ["--profile", str(pf), "--drive-root", str(drive_root), "--only-drift"]
    )
    assert rc2 == 5


@pytest.mark.requirement("CAT-02")
def test_remediate_missing_round_trip(
    fixture_canonical, drive_root, monkeypatch, tmp_path
) -> None:
    _apply(fixture_canonical, drive_root)
    # Delete one file so it shows as MISSING.
    (drive_root / "Alice" / "AA" / "One.mp3").unlink()

    # Fake the canonical loader for apply module.
    def fake_loader(*, playlist_names, use_shared_state=False, hash_cache=None, db=None):
        return fixture_canonical

    monkeypatch.setattr("apps.sync.usb.apply.load_canonical_tracks", fake_loader)
    monkeypatch.setattr("apps.sync.usb.verify.load_canonical_tracks", fake_loader)
    monkeypatch.setattr(apply_mod, "REVERSAL_DIR", tmp_path / "rev")

    pf = tmp_path / "profile.yaml"
    pf.write_text(
        """
name: fixtureA
drive_label: FIXTURE-A
playlists: [Warmup, Peak]
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: canonical-wins
""",
        encoding="utf-8",
    )
    rc = apply_mod.main(
        [
            "--profile",
            str(pf),
            "--drive-root",
            str(drive_root),
            "--remediate-drift",
            "--cautious",
            "--playlists",
            "Warmup",
        ]
    )
    assert rc == 0
    # The missing file is back.
    assert (drive_root / "Alice" / "AA" / "One.mp3").exists()


@pytest.mark.requirement("CAT-02")
def test_plan_from_verify_builds_rename_ops(
    fixture_canonical, drive_root
) -> None:
    _apply(fixture_canonical, drive_root)
    src = drive_root / "Alice" / "AA" / "One.mp3"
    dst = drive_root / "Relocated" / "One.mp3"
    dst.parent.mkdir(parents=True)
    src.rename(dst)
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert report.renamed == 1

    plan = plan_from_verify(
        profile=_profile(),
        canonical=fixture_canonical,
        report=report,
        drive_root=drive_root,
        restrict_statuses={FileStatus.RENAMED},
    )
    kinds = [op.kind for op in plan.ops]
    assert "rename" in kinds
