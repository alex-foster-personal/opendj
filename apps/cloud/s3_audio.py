"""Opt-in R2 audio uploader (NOT enabled by default).

CAT-04b: audio cold-start bucket. Phase 11 only ships the helpers; actual
enablement is a separate decision per user (cost model lives in
``apps/cloud/README.md``).

Minimal surface: :func:`upload_audio` takes a local path + remote key and
PUTs via the same :class:`S3Client` protocol used by :mod:`apps.cloud.lock`.

Intentionally synchronous + simple. Phase 12+ may swap in a parallel
worker pool once real volumes matter.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .config import CloudConfig
from .lock import S3Client


class AudioUploadError(RuntimeError):
    """Raised when an upload fails."""


def compute_content_hash(path: Path, chunk_size: int = 1 << 20) -> str:
    """Return a hex SHA-256 of the file at ``path``.

    Kept simple: full-file hash. Good enough for 16 GB of audio at ~400
    MB/s on an M-series SSD. Streaming reads keep memory flat.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def audio_object_key(stable_id: str, extension: str) -> str:
    """Return the R2 key for a given stable_id.

    Prefix format ``audio/<stable_id[:2]>/<stable_id><ext>`` to shard the
    bucket and keep the listing API reasonable once we have thousands of
    objects.
    """
    if not extension.startswith("."):
        extension = "." + extension
    return f"audio/{stable_id[:2]}/{stable_id}{extension}"


def upload_audio(
    cfg: CloudConfig,
    s3: S3Client,
    local_path: Path,
    stable_id: str,
) -> str:
    """PUT the local audio file to ``cfg.audio_bucket``.

    Returns the object key. The caller is responsible for idempotency --
    this function always writes. :func:`compute_content_hash` can be used
    by callers to skip re-upload.
    """
    local_path = Path(local_path)
    if not local_path.exists():
        raise AudioUploadError(f"local audio missing: {local_path}")
    key = audio_object_key(stable_id, local_path.suffix)
    body = local_path.read_bytes()
    ok, _etag = s3.put_object_if_match(cfg.audio_bucket, key, body, etag="*")
    if not ok:
        # Fall back to unconditional create.
        created, _etag = s3.put_object_if_none_match(
            cfg.audio_bucket, key, body
        )
        if not created:
            raise AudioUploadError(
                f"audio upload failed for {key} (exists + no etag)"
            )
    return key


__all__ = [
    "AudioUploadError",
    "audio_object_key",
    "compute_content_hash",
    "upload_audio",
]
