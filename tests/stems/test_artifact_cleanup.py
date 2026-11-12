"""Regression for stem bundle artifact cleanup classification (issue #13163).

[if] an artifact is a required manifest input [then] classification keeps it required, [else stop].
[if] an artifact is a stale derived output [then] classification marks it stale, [else stop].
[if] an artifact path escapes its bundle root [then] validation raises StemArtifactError, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.stems.artifacts import (
    StemArtifactError,
    StemManifest,
    classify_bundle_top_level_artifact,
    resolve_manifest_stem_path,
)
from apps.stems.constants import STEM_MANIFEST_FILENAME

pytestmark = [pytest.mark.requirement("STEM-13486")]

_SHA = "a" * 64


def _demucs_manifest(*, files: dict[str, str] | None = None) -> StemManifest:
    declared = files or {
        "vocals": "vocals.wav",
        "drums": "drums.wav",
        "bass": "bass.wav",
        "other": "other.wav",
    }
    return StemManifest.model_validate(
        {
            "schema_version": 1,
            "stable_id": "track-001",
            "model": {"name": "htdemucs", "version": "4.0.1"},
            "source": {"path": "/music/source.flac", "sha256": _SHA},
            "files": declared,
        }
    )


def _touch(path: Path) -> None:
    path.write_bytes(b"")


def test_required_manifest_inputs_are_not_cleanup_candidates(tmp_path: Path) -> None:
    """Required manifest inputs stay required so cleanup cannot delete them."""
    manifest = _demucs_manifest()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _touch(bundle / STEM_MANIFEST_FILENAME)
    for name in manifest.files.values():
        _touch(bundle / name)

    assert classify_bundle_top_level_artifact(bundle, STEM_MANIFEST_FILENAME, manifest=manifest) == "required"
    for name in manifest.files.values():
        assert classify_bundle_top_level_artifact(bundle, name, manifest=manifest) == "required"


def test_stale_part_file_not_in_manifest_is_a_cleanup_candidate(tmp_path: Path) -> None:
    """A leftover part file not named in the manifest is stale."""
    manifest = _demucs_manifest(
        files={
            "vocals": "vocals.flac",
            "drums": "drums.flac",
            "bass": "bass.flac",
            "other": "other.flac",
        }
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _touch(bundle / "vocals.flac")
    _touch(bundle / "vocals.mp3")

    assert classify_bundle_top_level_artifact(bundle, "vocals.mp3", manifest=manifest) == "stale"
    assert classify_bundle_top_level_artifact(bundle, "vocals.flac", manifest=manifest) == "required"


def test_carry_over_sidecar_is_not_stale(tmp_path: Path) -> None:
    """Non-part sidecars stay required like publish_bundle carry-over."""
    manifest = _demucs_manifest()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _touch(bundle / "waveform.json")

    assert classify_bundle_top_level_artifact(bundle, "waveform.json", manifest=manifest) == "required"


@pytest.mark.parametrize(
    "declared",
    [
        "../evil.wav",
        "/vocals.wav",
        "vocals.txt",
    ],
)
def test_manifest_stem_path_outside_bundle_raises(tmp_path: Path, declared: str) -> None:
    """Manifest stem paths that escape the bundle root fail closed."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with pytest.raises(StemArtifactError):
        resolve_manifest_stem_path(bundle, declared, "vocals")
