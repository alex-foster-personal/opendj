"""Build check for bundled demo-track licence provenance.

- [if] a demo-track file is listed with complete licence fields [then] check passes, [else stop]
- [if] a bundled demo-track file has no manifest entry [then] check fails, [else stop]
- [if] a manifest entry lacks licence source or attribution [then] check fails, [else stop]
- [if] the manifest references a missing media file [then] check fails, [else stop]
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from apps.onboarding import contract as demo_tracks
from scripts import check_demo_track_licences as check_script

pytestmark = pytest.mark.requirement("LIBRARY-HYGIENE-13506")


def _write_manifest(path: Path, tracks: list[dict]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "tracks": tracks}, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_silent_wav(path: Path, *, duration_s: float = 0.05) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 8000
    frames = int(rate * duration_s)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * frames)


def _fixture_bundle(tmp_path: Path) -> tuple[Path, Path]:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = tmp_path / "demo-tracks.manifest.json"
    return bundle, manifest


def test_complete_manifest_entry_passes(tmp_path: Path) -> None:
    """[if] complete licence fields [then] check passes, [else stop]."""
    bundle, manifest = _fixture_bundle(tmp_path)
    _write_silent_wav(bundle / "clip.wav")
    _write_manifest(
        manifest,
        [
            {
                "id": "clip",
                "media": "clip.wav",
                "licence_source": "https://creativecommons.org/publicdomain/zero/1.0/",
                "attribution": "Open DJ silent fixture (CC0)",
            }
        ],
    )
    assert demo_tracks.verify_demo_track_licences(bundle_root=bundle, manifest_path=manifest) == []
    assert check_script._run_check(bundle, manifest) == 0


def test_bundled_file_without_manifest_entry_fails(tmp_path: Path) -> None:
    """[if] bundled file has no manifest entry [then] check fails, [else stop]."""
    bundle, manifest = _fixture_bundle(tmp_path)
    _write_silent_wav(bundle / "orphan.wav")
    _write_manifest(manifest, [])
    errors = demo_tracks.verify_demo_track_licences(bundle_root=bundle, manifest_path=manifest)
    assert errors
    assert any("orphan.wav" in line and "no manifest entry" in line for line in errors)
    assert check_script._run_check(bundle, manifest) == 1


def test_manifest_entry_missing_licence_fields_fails(tmp_path: Path) -> None:
    """[if] entry lacks licence source or attribution [then] check fails, [else stop]."""
    bundle, manifest = _fixture_bundle(tmp_path)
    _write_silent_wav(bundle / "clip.wav")
    _write_manifest(
        manifest,
        [
            {
                "id": "clip",
                "media": "clip.wav",
                "licence_source": "",
                "attribution": "Artist",
            }
        ],
    )
    with pytest.raises(demo_tracks.DemoTrackLicenceError):
        demo_tracks.load_manifest(manifest)
    _write_manifest(
        manifest,
        [
            {
                "id": "clip",
                "media": "clip.wav",
                "licence_source": "https://example.com/license",
                "attribution": "",
            }
        ],
    )
    with pytest.raises(demo_tracks.DemoTrackLicenceError):
        demo_tracks.load_manifest(manifest)


def test_manifest_references_missing_media_fails(tmp_path: Path) -> None:
    """[if] manifest references missing media [then] check fails, [else stop]."""
    bundle, manifest = _fixture_bundle(tmp_path)
    _write_manifest(
        manifest,
        [
            {
                "id": "ghost",
                "media": "missing.wav",
                "licence_source": "https://example.com/license",
                "attribution": "Nobody",
            }
        ],
    )
    errors = demo_tracks.verify_demo_track_licences(bundle_root=bundle, manifest_path=manifest)
    assert errors == ["manifest entry 'ghost' references missing media 'missing.wav'"]
    assert check_script._run_check(bundle, manifest) == 1


def test_repo_demo_track_bundle_passes() -> None:
    """[if] the repo bundle and manifest agree [then] check passes, [else stop]."""
    errors = demo_tracks.verify_demo_track_licences()
    assert errors == []
    assert check_script._run_check(demo_tracks.BUNDLE_ROOT, demo_tracks.MANIFEST_PATH) == 0
