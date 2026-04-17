"""Integration tests for apps.sync.usb.apply (CLI entrypoint + flow)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.sync.usb import apply as apply_mod
from apps.sync.usb import plan as plan_mod
from apps.sync.usb.state import CanonicalTrack


def _canon_from_source_tree(make_track) -> list[CanonicalTrack]:
    return [
        make_track(title="One", artist="Alice", album="AA", playlist="Warmup"),
        make_track(title="Two", artist="Bob", album="BB", playlist="Warmup"),
        make_track(title="Three", artist="Carol", album="CC", playlist="Peak"),
    ]


@pytest.fixture
def profile_file(tmp_path: Path) -> Path:
    p = tmp_path / "profile.yaml"
    p.write_text(
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
    return p


@pytest.fixture
def patched_state(monkeypatch, make_track):
    tracks = _canon_from_source_tree(make_track)

    def fake_loader(*, playlist_names, use_shared_state=False, hash_cache=None, db=None):
        wanted = set(playlist_names)
        return [t for t in tracks if t.playlist in wanted]

    monkeypatch.setattr(
        "apps.sync.usb.apply.load_canonical_tracks",
        fake_loader,
    )
    monkeypatch.setattr(
        "apps.sync.usb.plan.load_canonical_tracks",
        fake_loader,
    )
    monkeypatch.setattr(
        "apps.sync.usb.verify.load_canonical_tracks",
        fake_loader,
    )
    return tracks


@pytest.fixture
def isolated_reversal_dir(monkeypatch, tmp_path) -> Path:
    r = tmp_path / "reversal"
    monkeypatch.setattr(apply_mod, "REVERSAL_DIR", r)
    return r


@pytest.mark.requirement("CAT-02")
def test_plan_cli_exit_0(profile_file, drive_root, patched_state, capsys) -> None:
    rc = plan_mod.main(
        ["--profile", str(profile_file), "--drive-root", str(drive_root)]
    )
    assert rc == 0


@pytest.mark.requirement("CAT-02")
def test_plan_cli_json_output(
    profile_file, drive_root, patched_state, tmp_path
) -> None:
    out = tmp_path / "plan.json"
    rc = plan_mod.main(
        [
            "--profile",
            str(profile_file),
            "--drive-root",
            str(drive_root),
            "--json",
            str(out),
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["profile_name"] == "fixtureA"
    assert len(data["ops"]) == 3


@pytest.mark.requirement("CAT-02")
def test_apply_refuses_without_mode(
    profile_file, drive_root, patched_state, isolated_reversal_dir
) -> None:
    rc = apply_mod.main(
        ["--profile", str(profile_file), "--drive-root", str(drive_root)]
    )
    assert rc == 3


@pytest.mark.requirement("CAT-02")
def test_apply_cautious_filters_playlists(
    profile_file, drive_root, patched_state, isolated_reversal_dir
) -> None:
    rc = apply_mod.main(
        [
            "--profile",
            str(profile_file),
            "--drive-root",
            str(drive_root),
            "--cautious",
            "--playlists",
            "Peak",
            "--skip-check",
            "no_other_writer",
        ]
    )
    assert rc == 0
    # Only Carol/CC/Three.mp3 was copied.
    assert (drive_root / "Carol" / "CC" / "Three.mp3").exists()
    assert not (drive_root / "Alice" / "AA" / "One.mp3").exists()


@pytest.mark.requirement("CAT-02")
def test_apply_bulk_idempotent(
    profile_file, drive_root, patched_state, isolated_reversal_dir
) -> None:
    rc = apply_mod.main(
        [
            "--profile",
            str(profile_file),
            "--drive-root",
            str(drive_root),
            "--i-understand-the-risks",
        ]
    )
    assert rc == 0
    for rel in ("Alice/AA/One.mp3", "Bob/BB/Two.mp3", "Carol/CC/Three.mp3"):
        assert (drive_root / rel).exists()
    # M3u8 written.
    assert (drive_root / "Playlists" / "Warmup.m3u8").exists()
    # Marker written.
    assert (drive_root / ".mdj-marker.json").exists()

    # Re-run should be a no-op.
    rc2 = apply_mod.main(
        [
            "--profile",
            str(profile_file),
            "--drive-root",
            str(drive_root),
            "--i-understand-the-risks",
        ]
    )
    assert rc2 == 0


@pytest.mark.requirement("CAT-02")
def test_apply_reversal_script_is_valid_bash(
    profile_file, drive_root, patched_state, isolated_reversal_dir
) -> None:
    rc = apply_mod.main(
        [
            "--profile",
            str(profile_file),
            "--drive-root",
            str(drive_root),
            "--i-understand-the-risks",
            "--reason",
            "unit-test",
        ]
    )
    assert rc == 0
    scripts = list(isolated_reversal_dir.glob("reversal-*.sh"))
    assert scripts, f"no reversal script written in {isolated_reversal_dir}"
    from apps.sync.usb.reversal import verify_syntax
    ok, err = verify_syntax(scripts[0])
    assert ok, f"bash -n failed: {err}"
    # Header includes profile + mode + reason.
    text = scripts[0].read_text()
    assert "profile:    fixtureA" in text
    assert "mode:       bulk" in text
    assert "reason:     unit-test" in text
