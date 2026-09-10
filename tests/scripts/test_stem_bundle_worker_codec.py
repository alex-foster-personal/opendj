"""Guard: scripts/stem_bundle_worker.py's stem output codec follows the
source-extension policy (apps/stems/stem_size_policy.py) instead of the old
hardcoded WAV output (issue #1497).

_stem_file_suffix and _manifest_dict are pure (no filesystem access, no
torch/demucs) so this guard runs on any box, with no GPU and no real audio
fixture required -- the full separation path (write_bundle) needs a
GPU/torch/demucs stack this suite does not have. The manifest round-trip
tests below DO touch the filesystem (stub stem files + manifest.json under
tmp_path) but only to exercise the real production reader
(apps.webui.server.stem_artifacts.load_stem_bundle) against the schema v3
manifest shape this worker writes, since that reader is the actual contract
this fix must satisfy and _load_v3_bundle never reads file headers (audio
alignment comes from the manifest's own audio block, not the files).

  - [if] the source extension is .mp3 [then] the stem suffix is .mp3, [else stop]
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.stem_bundle_worker as worker
from apps.stems.stem_size_policy import UnknownSourceFormatError
from apps.webui.server.stem_artifacts import load_stem_bundle

pytestmark = pytest.mark.requirement("STEM-02")


def test_mp3_source_gets_mp3_stem_suffix() -> None:
    assert worker._stem_file_suffix(Path("track.mp3")) == ".mp3"


def test_m4a_source_gets_mp3_stem_suffix() -> None:
    assert worker._stem_file_suffix(Path("track.m4a")) == ".mp3"


def test_flac_source_gets_flac_stem_suffix() -> None:
    assert worker._stem_file_suffix(Path("track.flac")) == ".flac"


def test_wav_source_gets_flac_stem_suffix() -> None:
    assert worker._stem_file_suffix(Path("track.wav")) == ".flac"


def test_unrecognised_source_extension_raises() -> None:
    with pytest.raises(UnknownSourceFormatError):
        worker._stem_file_suffix(Path("track.ogg"))


def _write_stub_bundle(tmp_path: Path, stable_id: str, *, ext: str, codec: str) -> Path:
    """Write a schema v3 bundle (stub part bytes, real manifest.json) so the
    production reader can be exercised without a GPU or real audio."""
    bundle_dir = tmp_path / stable_id
    bundle_dir.mkdir()
    for name in worker.STEM_PARTS:
        (bundle_dir / f"{name}{ext}").write_bytes(b"not-real-audio")
    manifest = worker._manifest_dict(
        stable_id=stable_id,
        audio_path=Path(f"/tmp/source{'.mp3' if codec == 'mp3' else '.flac'}"),
        source_sha256="0" * 64,
        ext=ext,
        output_codec=codec,
        audio={"sample_rate": 44100, "frame_count": 1000, "channels": 2},
        size_policy={
            "source_bytes": 12345,
            "source_kbps": 256.0,
            "mp3_settings": None,
            "part_bytes": {name: 15 for name in worker.STEM_PARTS},
            "violations": [],
        },
    )
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def test_mp3_schema_v3_manifest_round_trips_through_real_reader(tmp_path: Path) -> None:
    root = _write_stub_bundle(tmp_path, "teststem-mp3", ext=".mp3", codec="mp3")
    bundle = load_stem_bundle("teststem-mp3", roots=[root])
    assert bundle.media_type == "audio/mpeg"
    assert bundle.layout == "demucs4"


def test_flac_schema_v3_manifest_round_trips_through_real_reader(tmp_path: Path) -> None:
    root = _write_stub_bundle(tmp_path, "teststem-flac", ext=".flac", codec="flac")
    bundle = load_stem_bundle("teststem-flac", roots=[root])
    assert bundle.media_type == "audio/flac"
    assert bundle.layout == "demucs4"
