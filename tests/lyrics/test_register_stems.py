"""register_stems: lyric-pipeline stems become canonical, readable bundles.

- if register_pair leaves a bundle the strict loader cannot read back then
  stems are 'registered' but still lost -- broken
- if mismatched parts (codec or length) register anyway then the deck gets a
  falsely-aligned stem set -- broken
- if a failed registration leaves a partial bundle dir behind then the next
  sweep reports 'already' for garbage -- broken
"""

from __future__ import annotations

import json
import math
import shutil
import wave
from pathlib import Path

import pytest

# A capability report, not a pass (Codex on #6821): the CI lanes that run
# this file (ci.yml's test, fast and contracts jobs) install ffmpeg and fail
# their preflight without it (scripts/ci_runner_preflight.sh), so there these
# tests always run.
pytestmark = pytest.mark.skipif(
    shutil.which("ffprobe") is None,
    reason="UNAVAILABLE: ffprobe is not on PATH, so register_pair (and its "
           "bundle-lock acceptance) is unmeasured here. CI installs ffmpeg.",
)

from apps.lyrics.register_stems import (  # noqa: E402
    RegisterPairStorage,
    register_pair,
)
from apps.stems.artifacts import load_stem_bundle  # noqa: E402

from .conftest import use_local_mode  # noqa: E402


@pytest.fixture(autouse=True)
def _local_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    use_local_mode(monkeypatch)


def _write_wav(path: Path, seconds: float, sample_rate: int = 8000) -> None:
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(
            b"".join(
                int(3000 * math.sin(i / 20)).to_bytes(2, "little", signed=True) * 2
                for i in range(frames)
            )
        )


# REQ: LYR-02
def test_register_pair_stamps_files_sha256_on_v3_manifest(tmp_path: Path) -> None:
    _write_wav(tmp_path / "x-vocals.wav", 1.0)
    _write_wav(tmp_path / "x-instrumental.wav", 1.0)
    root = tmp_path / "root"
    out = register_pair(
        stable_id="sid-hash",
        vocals=tmp_path / "x-vocals.wav",
        instrumental=tmp_path / "x-instrumental.wav",
        model_name="m",
        model_version="v",
        source_path=str(tmp_path / "x-vocals.wav"),
        storage=RegisterPairStorage(root=root),
    )
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["schema_version"] == 3
    assert set(manifest["files_sha256"]) == {"vocals", "instrumental"}
    import hashlib

    for part, rel in manifest["files"].items():
        digest = hashlib.sha256((out / rel).read_bytes()).hexdigest()
        assert manifest["files_sha256"][part] == digest
    assert (tmp_path / "x-vocals.wav").is_file()
    assert (tmp_path / "x-instrumental.wav").is_file()


def test_register_pair_roundtrips_through_the_strict_loader(tmp_path: Path) -> None:
    _write_wav(tmp_path / "x-vocals.wav", 1.0)
    _write_wav(tmp_path / "x-instrumental.wav", 1.0)
    root = tmp_path / "root"

    out = register_pair(
        stable_id="sid001",
        vocals=tmp_path / "x-vocals.wav",
        instrumental=tmp_path / "x-instrumental.wav",
        model_name="mel-band-roformer-test",
        model_version="ckpt-test",
        source_path=str(tmp_path / "x-vocals.wav"),
        storage=RegisterPairStorage(root=root),
    )
    assert out == root / "sid001"
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["layout"] == "roformer2"
    assert manifest["audio"]["sample_rate"] == 8000

    bundle = load_stem_bundle("sid001", stems_dir=root)
    assert set(bundle.files) == {"vocals", "instrumental"}
    assert bundle.alignment.sample_rate == 8000


def test_register_pair_refuses_misaligned_parts_and_leaves_nothing(tmp_path: Path) -> None:
    _write_wav(tmp_path / "y-vocals.wav", 1.0)
    _write_wav(tmp_path / "y-instrumental.wav", 2.0)  # 1s apart >> tolerance
    root = tmp_path / "root"

    with pytest.raises(ValueError, match="frame counts differ"):
        register_pair(
            stable_id="sid002",
            vocals=tmp_path / "y-vocals.wav",
            instrumental=tmp_path / "y-instrumental.wav",
            model_name="m", model_version="v",
            source_path="unknown", storage=RegisterPairStorage(root=root),
        )
    assert not (root / "sid002").exists(), "no partial bundle may remain"


def test_register_pair_refuses_mixed_codecs(tmp_path: Path) -> None:
    _write_wav(tmp_path / "z-vocals.wav", 1.0)
    (tmp_path / "z-instrumental.flac").write_bytes(b"fLaC junk")
    with pytest.raises(ValueError, match="mix codecs"):
        register_pair(
            stable_id="sid003",
            vocals=tmp_path / "z-vocals.wav",
            instrumental=tmp_path / "z-instrumental.flac",
            model_name="m", model_version="v",
            source_path="unknown", storage=RegisterPairStorage(root=tmp_path / "root"),
        )


def test_batch1_is_registrable_via_the_tracks_json_join(tmp_path: Path, monkeypatch) -> None:
    """batch1 follows the crate pattern: stable_ids come from tracks.json.

    - if batch1 stems cannot be registered then generated stems are flat-only
      and lost to the product -- broken
    - if a row without a stable_id is guessed instead of skipped -- broken
    """
    from apps.lyrics import register_stems as rs

    assert "batch1" in rs.REGISTRABLE_CORPORA
    assert "batch1" in rs.TRACKS_JSON_CORPORA

    corpus_dir = tmp_path / "batch1"
    corpus_dir.mkdir()
    (corpus_dir / "tracks.json").write_text(json.dumps([
        {"track_id": "batch1000", "stable_id": "sidA", "path": "/x/a.mp3"},
        {"track_id": "batch1001", "source_path": "/x/b.mp3"},  # no stable_id
    ]), encoding="utf-8")
    monkeypatch.setattr(rs, "LYRICS_EVAL_DIR", tmp_path)

    mapping = rs._tracks_json_stable_ids("batch1")
    assert mapping == {"batch1000": ("sidA", "/x/a.mp3")}


def test_sweep_corpus_leaves_existing_v1_manifest_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing v1 bundles must never be rewritten by the sweep."""
    from apps.lyrics import register_stems as rs

    eval_dir = tmp_path / "lyrics-eval"
    corpus = "crate"
    stems_dir = eval_dir / corpus / "stems"
    stems_dir.mkdir(parents=True)
    _write_wav(stems_dir / "crate000-vocals.wav", 1.0)
    _write_wav(stems_dir / "crate000-instrumental.wav", 1.0)
    (eval_dir / corpus / "tracks.json").write_text(
        json.dumps([{
            "track_id": "crate000",
            "stable_id": "sid-v1",
            "path": "/lib/track.wav",
        }]),
        encoding="utf-8",
    )
    monkeypatch.setattr(rs, "LYRICS_EVAL_DIR", eval_dir)
    root = tmp_path / "stems-root"
    bundle_dir = root / "sid-v1"
    bundle_dir.mkdir(parents=True)
    v1_manifest = {
        "schema_version": 1,
        "stable_id": "sid-v1",
        "layout": "demucs4",
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "/lib/track.wav", "sha256": "0" * 64},
        "files": {
            "vocals": "vocals.wav",
            "drums": "drums.wav",
            "bass": "bass.wav",
            "other": "other.wav",
        },
    }
    manifest_path = bundle_dir / "manifest.json"
    original = json.dumps(v1_manifest, indent=2) + "\n"
    manifest_path.write_text(original, encoding="utf-8")

    report = rs.sweep_corpus(corpus, write=True, root=root)
    assert report.already == ["sid-v1"]
    assert report.registered == []
    assert manifest_path.read_text(encoding="utf-8") == original


def test_batch2_is_registrable_via_the_tracks_json_join(tmp_path: Path, monkeypatch) -> None:
    """batch2 (rung 2 of the ladder) follows the same tracks.json pattern.

    - if batch2 stems cannot be registered then rung-2 generated stems are
      flat-only and lost to the product -- broken
    - if a row without a stable_id is guessed instead of skipped -- broken
    """
    from apps.lyrics import register_stems as rs

    assert "batch2" in rs.REGISTRABLE_CORPORA
    assert "batch2" in rs.TRACKS_JSON_CORPORA

    corpus_dir = tmp_path / "batch2"
    corpus_dir.mkdir()
    (corpus_dir / "tracks.json").write_text(json.dumps([
        {"track_id": "batch2000", "stable_id": "sidB", "path": "/x/c.mp3"},
        {"track_id": "batch2001", "source_path": "/x/d.mp3"},  # no stable_id
    ]), encoding="utf-8")
    monkeypatch.setattr(rs, "LYRICS_EVAL_DIR", tmp_path)

    mapping = rs._tracks_json_stable_ids("batch2")
    assert mapping == {"batch2000": ("sidB", "/x/c.mp3")}


def test_register_pair_into_a_data_dir_waits_for_the_bundle_lock(tmp_path: Path) -> None:
    """[if] another writer (the STEM-58 swap, eviction) holds this bundle's lock
    [then] register_pair publishes nothing until it is released, [else stop]
    (Codex P1 on #6820 and #6821).

    MUTATION TARGET: drop ``store_writer_lock`` from ``register_pair`` and the
    bundle appears while the lock is still held.
    """
    import threading
    import time

    from apps.cloud.stem_upload_queue import exclusive_file_lock
    from apps.stems.bundle_lock import bundle_lock_path

    _write_wav(tmp_path / "x-vocals.wav", 1.0)
    _write_wav(tmp_path / "x-instrumental.wav", 1.0)
    data_dir = tmp_path / "data"
    root = data_dir / "state" / "stems-roformer-spike"
    results: list[object] = []

    def run() -> None:
        try:
            results.append(
                register_pair(
                    stable_id="sid-locked",
                    vocals=tmp_path / "x-vocals.wav",
                    instrumental=tmp_path / "x-instrumental.wav",
                    model_name="m",
                    model_version="v",
                    source_path=str(tmp_path / "x-vocals.wav"),
                    storage=RegisterPairStorage(root=root),
                )
            )
        except BaseException as exc:  # noqa: BLE001 - the test reports any failure
            results.append(exc)

    with exclusive_file_lock(bundle_lock_path(data_dir, "sid-locked")):
        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(1.0)  # an unlocked register of two small parts takes milliseconds
        assert thread.is_alive() and not (root / "sid-locked").exists(), (
            "register_pair published while another writer held the lock"
        )
    thread.join(timeout=60)

    assert results == [root / "sid-locked"]
    load_stem_bundle("sid-locked", stems_dir=root)
