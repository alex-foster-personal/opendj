"""Keep the stem download index matched to on-disk bundles (STEM-61).

A local rewrite (new separation, or a FLAC-to-MP3 recompress) must call
:func:`record_converted_bundle` in the same step as the swap. This module
never hand-edits ``state/stem-bundle-index.json``; every write goes through
:func:`apps.cloud.stem_index.save_cached_index`.

When a converted bundle still carries the STEM-51 fallback note
(``codec_fallback`` saying lossless FLAC was written) but its ``files`` map
already names ``.mp3`` parts, the descriptive ``size_policy`` fields are
rewritten before the index is hashed, so the cached manifest digest is the
post-fix file.

``python -m apps.stems.recompress`` calls :func:`record_converted_bundle`
after each successful live swap. The local demucs worker
(``scripts/stem_bundle_worker.py``) calls
:func:`record_published_bundle_if_shared` after each live publish into a
shared stem root. Bundles converted or published before that wiring still
need ``python -m apps.cloud.stem_index rebuild`` on the host.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apps.cloud.asset_store import (
    AssetS3Client,
    AssetStoreError,
    asset_object_key,
    compute_asset_hash,
    object_exists,
    push_asset,
)
from apps.cloud.config import CloudConfig
from apps.cloud.lock import S3Client
from apps.cloud.stem_index import (
    MANIFEST_FILENAME,
    StemAssetIndex,
    StemFileHashes,
    StemIndexError,
    local_stem_roots,
    publish_index,
    rebuild_cached_index_from_local_manifests,
    sync_bundle_into_cached_index,
)

_STALE_FLAC_FALLBACK = "wrote lossless flac"


def _mp3_settings_from_source(raw: dict[str, Any], files: dict[str, Any]) -> dict[str, str] | None:
    """Ladder settings from the recorded source, or None when it is not here."""
    source = raw.get("source")
    audio = raw.get("audio")
    if not isinstance(source, dict) or not isinstance(audio, dict):
        return None
    path = Path(str(source.get("path") or ""))
    if not path.is_file():
        return None
    try:
        frame_count = int(audio["frame_count"])
        sample_rate = int(audio["sample_rate"])
    except (KeyError, TypeError, ValueError):
        return None
    if frame_count <= 0 or sample_rate <= 0:
        return None
    from apps.stems import stem_size_policy as pol

    source_kbps = pol.effective_bitrate_kbps(path.stat().st_size, frame_count / sample_rate)
    settings = pol.mp3_lame_settings_for_source_kbps(source_kbps)
    return {part: settings for part in files if isinstance(part, str)}


def _read_manifest_dict(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


def _mp3_files_map(raw: dict[str, Any]) -> dict[str, Any] | None:
    files = raw.get("files")
    if not isinstance(files, dict) or not files:
        return None
    declared = [name for name in files.values() if isinstance(name, str)]
    if not declared or {Path(name).suffix.lower() for name in declared} != {".mp3"}:
        return None
    return files


def refresh_stale_converted_manifest(bundle_dir: Path) -> bool:
    """Fix descriptive ``size_policy`` fields after a FLAC-to-MP3 rewrite.

    Returns True when ``manifest.json`` was rewritten. Only the contradictory
    STEM-51 fallback (MP3 parts still claiming lossless FLAC) is in scope.
    """
    path = Path(bundle_dir) / MANIFEST_FILENAME
    raw = _read_manifest_dict(path)
    if raw is None:
        return False
    files = _mp3_files_map(raw)
    if files is None:
        return False
    policy = raw.get("size_policy")
    if not isinstance(policy, dict):
        policy = {}
    fallback = policy.get("codec_fallback")
    stale_fallback = isinstance(fallback, str) and _STALE_FLAC_FALLBACK in fallback.lower()
    stale_settings = policy.get("mp3_settings") is None
    if not stale_fallback and not stale_settings and raw.get("codec") == "mp3":
        return False
    changed = False
    if stale_fallback:
        policy["codec_fallback"] = None
        policy["policy_codec"] = "mp3"
        changed = True
    if stale_settings:
        filled = _mp3_settings_from_source(raw, files)
        if filled is not None:
            policy["mp3_settings"] = filled
            changed = True
    if raw.get("codec") != "mp3":
        raw["codec"] = "mp3"
        changed = True
    if not changed:
        return False
    raw["size_policy"] = policy
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return True


def record_converted_bundle(data_dir: Path, bundle_dir: Path) -> StemFileHashes:
    """Conversion last step: refresh stale policy fields, then upsert the index."""
    refresh_stale_converted_manifest(bundle_dir)
    return sync_bundle_into_cached_index(data_dir, bundle_dir)


def record_published_bundle_if_shared(bundle_dir: Path) -> StemFileHashes | None:
    """After a live bundle swap, upsert the download index when ``bundle_dir`` is shared storage."""
    from apps.stems.bundle_lock import data_dir_of_bundle, is_shared_store_bundle

    bundle_dir = Path(bundle_dir)
    if not is_shared_store_bundle(bundle_dir):
        return None
    return record_converted_bundle(data_dir_of_bundle(bundle_dir), bundle_dir)


def _local_stem_file(data_dir: Path, stable_id: str, filename: str) -> Path | None:
    """Return a regular on-disk part path, with later ``local_stem_roots`` winning."""
    for root in reversed(local_stem_roots(data_dir)):
        if not root.is_dir():
            continue
        path = root / stable_id / filename
        if path.is_file() and not path.is_symlink():
            return path
    return None


def ensure_index_assets_remote(
    cfg: CloudConfig,
    assets: AssetS3Client,
    data_dir: Path,
    entries: StemAssetIndex,
) -> None:
    """HEAD every digest in ``entries``. Upload missing objects from disk.

    Raises :class:`StemIndexError` naming every still-absent ``assets/<xx>/<digest>``
    key. Does not touch the shared pointer.
    """
    absent: list[str] = []
    for stable_id, file_hashes in entries.items():
        for _filename, digest in file_hashes.items():
            local_path = _local_stem_file(data_dir, stable_id, _filename)
            if local_path is not None:
                on_disk = compute_asset_hash(local_path)
                if on_disk != digest:
                    absent.append(asset_object_key(digest))
                    continue
                key = asset_object_key(digest)
                try:
                    pushed = push_asset(cfg, assets, local_path)
                except AssetStoreError as exc:
                    raise StemIndexError(
                        f"upload failed for {local_path} (expected {key}): {exc}"
                    ) from exc
                if pushed.content_hash != digest:
                    absent.append(asset_object_key(digest))
                    continue
            if not object_exists(cfg, assets, digest):
                absent.append(asset_object_key(digest))
    if absent:
        keys = ", ".join(sorted(set(absent)))
        raise StemIndexError(
            f"stem index publish refused: remote object(s) absent ({keys}); "
            "shared pointer was not updated"
        )


def publish_verified_on_disk_index(
    cfg: CloudConfig,
    assets: AssetS3Client,
    index_s3: S3Client,
    data_dir: Path,
    entries: StemAssetIndex,
) -> str:
    """Upload/HEAD every referenced object, then CAS-merge ``entries`` once."""
    ensure_index_assets_remote(cfg, assets, data_dir, entries)
    return publish_index(cfg, index_s3, entries)


def rebuild_host_index(data_dir: Path) -> tuple[StemAssetIndex, Path, int]:
    """Refresh stale converted manifests, then rebuild this host's cache.

    The single command behind ``python -m apps.cloud.stem_index rebuild``.
    Returns the upserted entries, the cache path, and how many manifests
    were rewritten.
    """
    from apps.cloud.stem_index import local_stem_roots

    refreshed = 0
    for root in local_stem_roots(data_dir):
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if (
                child.is_dir()
                and (child / MANIFEST_FILENAME).is_file()
                and refresh_stale_converted_manifest(child)
            ):
                refreshed += 1
    entries, path = rebuild_cached_index_from_local_manifests(data_dir)
    return entries, path, refreshed


__all__ = [
    "ensure_index_assets_remote",
    "publish_verified_on_disk_index",
    "rebuild_host_index",
    "record_converted_bundle",
    "record_published_bundle_if_shared",
    "refresh_stale_converted_manifest",
]
