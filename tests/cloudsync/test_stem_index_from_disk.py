"""On-disk stem index rebuild after a FLAC-to-MP3 rewrite (STEM-61).

[if] a converted bundle is indexed [then] names and manifest sha256 match disk, [else stop].

Synthetic fixture bytes only: no library audio, no copyrighted material.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from apps.cloud.config import CloudConfig
from apps.cloud.stem_index import (
    MANIFEST_FILENAME,
    build_parser,
    entry_from_bundle_dir,
    load_cached_index,
    save_cached_index,
    sync_bundle_into_cached_index,
)
from apps.cloud.stem_index_sync import refresh_stale_converted_manifest
from apps.stems import stem_size_policy

pytestmark = pytest.mark.requirement("STEM-61")

_PARTS = ("vocals", "drums", "bass", "other")
_OLD_MANIFEST_SHA = "a" * 64
_OLD_FLAC_SHA = "b" * 64


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_mp3_bundle(
    stems_dir: Path,
    stable_id: str,
    *,
    source_path: Path | None = None,
    stale_policy: bool = False,
) -> Path:
    """Tiny synthetic bundle: dummy part bytes + a v3 files map naming .mp3."""
    bundle = stems_dir / stable_id
    bundle.mkdir(parents=True)
    files = {}
    for part in _PARTS:
        name = f"{part}.mp3"
        (bundle / name).write_bytes(f"synthetic-{stable_id}-{part}\n".encode("ascii"))
        files[part] = name
    policy: dict[str, object] = {
        "source_bytes": 12_000,
        "source_kbps": 256.0,
        "mp3_settings": None if stale_policy else {"vocals": "lame_v0_vbr"},
        "part_bytes": {part: 16 for part in _PARTS},
        "violations": [],
        "policy_codec": "mp3",
        "codec_fallback": (
            "policy codec mp3 needs ffmpeg (libmp3lame), which this install "
            "does not have; wrote lossless flac instead"
            if stale_policy
            else None
        ),
    }
    source = {
        "path": str(source_path) if source_path is not None else "/music/source.mp3",
        "sha256": "c" * 64,
    }
    (bundle / MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 3,
                "stable_id": stable_id,
                "layout": "demucs4",
                "model": {"name": "htdemucs", "version": "4.0.1"},
                "source": source,
                "files": files,
                "audio": {"sample_rate": 44_100, "frame_count": 44_100, "channels": 2},
                "codec": "mp3",
                "size_policy": policy,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return bundle


def _stale_flac_entry() -> dict[str, str]:
    return {
        MANIFEST_FILENAME: _OLD_MANIFEST_SHA,
        "vocals.flac": _OLD_FLAC_SHA,
        "drums.flac": _OLD_FLAC_SHA,
        "bass.flac": _OLD_FLAC_SHA,
        "other.flac": _OLD_FLAC_SHA,
    }


def test_converted_bundle_index_matches_on_disk_names_and_manifest_sha256(
    tmp_path: Path,
) -> None:
    """[if] a converted bundle is recorded [then] index names and sha256 match disk, [else stop]."""
    data_dir = tmp_path / "data"
    stems = data_dir / "state" / "stems"
    stable_id = "aabbccddeeff00112233445566778899aabbccdd"
    save_cached_index(data_dir, {stable_id: _stale_flac_entry()})
    bundle = _write_mp3_bundle(stems, stable_id)

    args = build_parser().parse_args(
        ["record", "--data-dir", str(data_dir), "--bundle-dir", str(bundle)]
    )
    assert int(args.func(args)) == 0

    expected = {MANIFEST_FILENAME: _sha256(bundle / MANIFEST_FILENAME)}
    expected.update({f"{part}.mp3": _sha256(bundle / f"{part}.mp3") for part in _PARTS})
    cached = load_cached_index(data_dir)
    assert cached[stable_id] == expected
    assert ".flac" not in "".join(cached[stable_id])
    assert cached[stable_id][MANIFEST_FILENAME] != _OLD_MANIFEST_SHA


def test_rebuild_index_upserts_on_disk_bundles_and_keeps_other_ids(
    tmp_path: Path,
) -> None:
    """[if] rebuild-index runs [then] on-disk ids update and others stay, [else stop]."""
    data_dir = tmp_path / "data"
    stems = data_dir / "state" / "stems"
    local_id = "1111111111111111111111111111111111111111"
    remote_id = "2222222222222222222222222222222222222222"
    save_cached_index(
        data_dir,
        {
            local_id: _stale_flac_entry(),
            remote_id: {MANIFEST_FILENAME: "d" * 64, "vocals.mp3": "e" * 64},
        },
    )
    _write_mp3_bundle(stems, local_id)

    args = build_parser().parse_args(["rebuild", "--data-dir", str(data_dir)])
    assert int(args.func(args)) == 0

    cached = load_cached_index(data_dir)
    local_entry = entry_from_bundle_dir(stems / local_id)
    assert cached[local_id] == local_entry
    assert cached[remote_id] == {MANIFEST_FILENAME: "d" * 64, "vocals.mp3": "e" * 64}


def test_stale_flac_fallback_is_cleared_before_the_index_hashes_the_manifest(
    tmp_path: Path,
) -> None:
    """[if] files are mp3 but fallback says FLAC [then] the note is cleared, [else stop]."""
    data_dir = tmp_path / "data"
    stems = data_dir / "state" / "stems"
    source = tmp_path / "source.mp3"
    source.write_bytes(b"x" * 40_000)
    stable_id = "3333333333333333333333333333333333333333"
    bundle = _write_mp3_bundle(stems, stable_id, source_path=source, stale_policy=True)
    before = json.loads((bundle / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert "wrote lossless flac" in before["size_policy"]["codec_fallback"]
    assert before["size_policy"]["mp3_settings"] is None

    assert refresh_stale_converted_manifest(bundle) is True
    after = json.loads((bundle / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert after["size_policy"]["codec_fallback"] is None
    # The ladder rung follows the whole-set rule (#8231), so derive it from the policy
    # the production code uses rather than pinning one bitrate.
    audio = before["audio"]
    source_kbps = stem_size_policy.effective_bitrate_kbps(
        source.stat().st_size, int(audio["frame_count"]) / int(audio["sample_rate"])
    )
    expected = stem_size_policy.mp3_lame_settings_for_source_kbps(source_kbps)
    assert after["size_policy"]["mp3_settings"] == {part: expected for part in _PARTS}

    entry = sync_bundle_into_cached_index(data_dir, bundle)
    assert entry[MANIFEST_FILENAME] == _sha256(bundle / MANIFEST_FILENAME)


@pytest.mark.requirement("STEM-8687")
def test_publish_refuses_when_manifest_digest_is_absent_and_keeps_pointer(
    tmp_path: Path, cfg: CloudConfig
) -> None:
    """[if] a referenced digest is absent [then] publish raises and the pointer stays, [else stop]."""
    from apps.cloud.asset_store import asset_object_key
    from apps.cloud.lock import FakeS3Client
    from apps.cloud.stem_index import StemIndexError, fetch_index, publish_index
    from apps.cloud.stem_index_sync import publish_verified_on_disk_index
    from tests.cloudsync.conftest import InMemoryAssetS3

    data_dir = tmp_path / "data"
    missing_digest = "0123456789abcdef" * 4
    stable_id = "5555555555555555555555555555555555555555"
    assets = InMemoryAssetS3()
    index_s3 = FakeS3Client()
    publish_index(cfg, index_s3, {"other": {MANIFEST_FILENAME: "f" * 64}})
    entries = {stable_id: {MANIFEST_FILENAME: missing_digest}}
    missing_key = asset_object_key(missing_digest)

    with pytest.raises(StemIndexError, match=missing_key):
        publish_verified_on_disk_index(cfg, assets, index_s3, data_dir, entries)

    assert fetch_index(cfg, index_s3) == {"other": {MANIFEST_FILENAME: "f" * 64}}
    assert (cfg.audio_bucket, missing_key) in assets.head_calls


@pytest.mark.requirement("STEM-8687")
def test_publish_uploads_missing_objects_then_direct_r2_fetches_them(
    tmp_path: Path, cfg: CloudConfig
) -> None:
    """[if] upload-then-publish succeeds [then] DirectR2Source fetches every referenced key, [else stop]."""
    from apps.cloud.asset_store import asset_object_key
    from apps.cloud.lock import FakeS3Client
    from apps.cloud.stem_index import fetch_index, publish_index
    from apps.cloud.stem_index_sync import publish_verified_on_disk_index
    from apps.cloud.stem_source import DirectR2Source
    from tests.cloudsync.conftest import InMemoryAssetS3

    data_dir = tmp_path / "data"
    stems = data_dir / "state" / "stems"
    stable_id = "4444444444444444444444444444444444444444"
    assets = InMemoryAssetS3()
    index_s3 = FakeS3Client()
    publish_index(cfg, index_s3, {"other": {MANIFEST_FILENAME: "f" * 64}})
    bundle = _write_mp3_bundle(stems, stable_id)
    entries = {stable_id: entry_from_bundle_dir(bundle)}

    etag = publish_verified_on_disk_index(cfg, assets, index_s3, data_dir, entries)
    assert etag

    published = fetch_index(cfg, index_s3)
    assert published["other"] == {MANIFEST_FILENAME: "f" * 64}
    assert published[stable_id] == entries[stable_id]
    for digest in entries[stable_id].values():
        assert assets.head_object(cfg.audio_bucket, asset_object_key(digest)) is not None

    source = DirectR2Source(cfg, assets)
    hydrated = tmp_path / "hydrated"
    source.fetch_bundle_files(
        stable_id=stable_id,
        file_hashes=entries[stable_id],
        tmp_dir=hydrated,
    )
    for filename in entries[stable_id]:
        assert (hydrated / filename).read_bytes() == (bundle / filename).read_bytes()
