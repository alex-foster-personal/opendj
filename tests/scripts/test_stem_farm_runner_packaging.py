"""Guard: scripts/stem_farm_runner.py packages the files a bundle's own
manifest.json declares, not a hardcoded set of .wav names (issue #1497).

stem_bundle_worker.py's output extension now follows
apps/stems/stem_size_policy.py's source-extension codec policy (flac or
mp3), so a packager that still assumed ``vocals.wav`` etc. would raise
FileNotFoundError on tar.add() for every non-wav-sourced bundle.
_bundle_tar_members is pure (no filesystem, no subprocess) so this guard
runs on any box.

  - [if] a v3 manifest declares mp3 files [then] the packager lists those
    .mp3 filenames, not .wav, [else stop]
"""

from __future__ import annotations

import scripts.stem_farm_runner as farm_runner


def test_mp3_manifest_yields_mp3_members() -> None:
    manifest = {
        "files": {
            "vocals": "vocals.mp3",
            "drums": "drums.mp3",
            "bass": "bass.mp3",
            "other": "other.mp3",
        }
    }
    members = farm_runner._bundle_tar_members(manifest)
    assert members == [
        "manifest.json",
        "vocals.mp3",
        "drums.mp3",
        "bass.mp3",
        "other.mp3",
    ]


def test_flac_manifest_yields_flac_members() -> None:
    manifest = {
        "files": {
            "vocals": "vocals.flac",
            "drums": "drums.flac",
            "bass": "bass.flac",
            "other": "other.flac",
        }
    }
    members = farm_runner._bundle_tar_members(manifest)
    assert all(name.endswith(".flac") for name in members[1:])
