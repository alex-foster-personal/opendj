"""Hash, verify and stream the bytes of a cloud offload (OFFLOAD, spec e8da14b8).

The one rule this module exists for (spec section 6): a local file may be
deleted only after R2 is shown to hold the same bytes. "Shown" means HEAD finds
the content-addressed object, its size equals the local size, and its content
matches. A single-part object's ETag is the MD5 of its body (R2 and S3 both;
the live test asserts it), so the content check is ``ETag == md5(local)``. An
ETag of any other shape (a multipart object an older rail wrote) is never
trusted blind: the object is streamed and its SHA-256 compared to the key.

Downloads stream to a temporary sibling, hash while writing, and are published
with ``os.link`` only when the digest matches, so a cancelled or corrupt restore
can never leave a file that looks real. An existing destination whose digest
matches the key is accepted without replacing it; if the digest differs the
destination is kept and the verified temp is discarded. Volumes that refuse hard
links classify an existing destination the same way and ``os.replace`` only
when the destination is absent (a small TOCTOU window remains on that branch).

-Claude
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from .asset_store import AssetS3Client, AssetStoreError, asset_object_key, require_credentials
from .config import CloudConfig

HASH_CHUNK_BYTES: int = 4 * 1024 * 1024
PLAIN_MD5_ETAG = re.compile(r"[0-9a-f]{32}")
RESTORE_TEMP_INFIX: str = ".offload-restore-"

ChunkProgress = Callable[[int], None]
CancelCheck = Callable[[], bool]

RestoreOutcome = Literal["published", "already_present", "preserved"]

_NO_HARD_LINK_ERRNOS = frozenset(
    {
        errno.EPERM,
        errno.ENOTSUP,
        errno.EXDEV,
        errno.EINVAL,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
)


class OffloadVerifyError(RuntimeError):
    """R2 does not provably hold the bytes; the local copy must stay."""


class OffloadCancelled(RuntimeError):
    """The operation was cancelled between chunks."""


class OffloadS3(AssetS3Client, Protocol):
    """The asset protocol plus a streaming read for large restores."""

    def download_to(
        self, bucket: str, key: str, handle: BinaryIO, on_chunk: ChunkProgress
    ) -> None:
        """Write the object body to ``handle``, calling ``on_chunk(n)`` per chunk."""


@dataclass(frozen=True)
class LocalDigest:
    """A local file's identity at the moment it was hashed."""

    path: Path
    sha256: str
    md5: str
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class RestorePublish:
    """How a verified restore landed at its destination."""

    outcome: RestoreOutcome
    bytes_written: int  # bytes streamed from R2; 0 when the GET was skipped


# --- local hashing ----------------------------------------------------------


def hash_local_file(path: Path, *, cancelled: CancelCheck | None = None) -> LocalDigest:
    """SHA-256 (the R2 key) and MD5 (the ETag check) of ``path`` in one read."""
    source = Path(path)
    before = source.stat()
    sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            if cancelled is not None and cancelled():
                raise OffloadCancelled(f"cancelled while hashing {source}")
            sha.update(chunk)
            md5.update(chunk)
    return LocalDigest(source, sha.hexdigest(), md5.hexdigest(), before.st_size, before.st_mtime_ns)


def unchanged_since_hash(digest: LocalDigest) -> bool:
    """True when the file still has the size and mtime it had when hashed."""
    try:
        now = digest.path.stat()
    except FileNotFoundError:
        return False
    return now.st_size == digest.size_bytes and now.st_mtime_ns == digest.mtime_ns


# --- remote verification ----------------------------------------------------


def _stream_sha256(s3: OffloadS3, bucket: str, key: str) -> str:
    sha = hashlib.sha256()

    class _HashSink:
        def write(self, chunk: bytes) -> int:
            sha.update(chunk)
            return len(chunk)

    s3.download_to(bucket, key, _HashSink(), lambda _n: None)  # type: ignore[arg-type]
    return sha.hexdigest()


def verify_remote_copy(cfg: CloudConfig, s3: OffloadS3, digest: LocalDigest) -> str:
    """Prove R2 holds ``digest``'s bytes; return HOW (``etag-md5`` / ``stream-sha256``).

    Raises :class:`OffloadVerifyError` naming the failed check. Never returns
    without one of the two content checks having passed.
    """
    require_credentials(cfg)
    key = asset_object_key(digest.sha256)
    try:
        head = s3.head_object(cfg.audio_bucket, key)
    except Exception as exc:  # transport failure is a failed verification, loudly
        raise OffloadVerifyError(f"HEAD {key} failed: {exc}") from exc
    if head is None:
        raise OffloadVerifyError(f"{key} is not in {cfg.audio_bucket}")
    if head.size != digest.size_bytes:
        raise OffloadVerifyError(
            f"{key} is {head.size} bytes in R2 but {digest.size_bytes} locally"
        )
    etag = head.etag.strip().strip('"').lower()
    if PLAIN_MD5_ETAG.fullmatch(etag):
        if etag != digest.md5:
            raise OffloadVerifyError(f"{key} ETag {etag} is not the local MD5 {digest.md5}")
        return "etag-md5"
    remote_sha = _stream_sha256(s3, cfg.audio_bucket, key)
    if remote_sha != digest.sha256:
        raise OffloadVerifyError(f"{key} streams to SHA-256 {remote_sha}, not its key")
    return "stream-sha256"


# --- restore download -------------------------------------------------------


def _require_digest(actual: str, expected: str, what: str) -> None:
    if actual != expected:
        raise OffloadVerifyError(f"{what}: downloaded SHA-256 {actual} is not {expected}; nothing replaced")


def restore_temp_path(dest: Path) -> Path:
    """Where a restore streams before the verified rename."""
    return dest.with_name(f"{dest.name}{RESTORE_TEMP_INFIX}{secrets.token_hex(4)}")


def _classify_existing_dest(
    dest: Path, expected_sha256: str, *, cancelled: CancelCheck
) -> RestoreOutcome:
    digest = hash_local_file(dest, cancelled=cancelled)
    if digest.sha256 == expected_sha256:
        return "already_present"
    return "preserved"


def _publish_verified_temp(
    temp: Path,
    target: Path,
    expected_sha256: str,
    written: int,
    *,
    cancelled: CancelCheck,
) -> RestorePublish:
    try:
        os.link(temp, target)
        temp.unlink(missing_ok=True)
        return RestorePublish("published", written)
    except FileExistsError:
        try:
            outcome = _classify_existing_dest(target, expected_sha256, cancelled=cancelled)
        except FileNotFoundError:
            os.replace(temp, target)
            return RestorePublish("published", written)
        temp.unlink(missing_ok=True)
        return RestorePublish(outcome, written)
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINK_ERRNOS:
            raise
        if target.exists():
            outcome = _classify_existing_dest(target, expected_sha256, cancelled=cancelled)
            temp.unlink(missing_ok=True)
            return RestorePublish(outcome, written)
        os.replace(temp, target)
        return RestorePublish("published", written)


def download_verified(
    cfg: CloudConfig,
    s3: OffloadS3,
    sha256: str,
    dest: Path,
    *,
    on_chunk: ChunkProgress,
    cancelled: CancelCheck,
) -> RestorePublish:
    """Stream ``sha256``'s object to ``dest`` when needed; verify before publish.

    The body lands in a temp sibling, is checked against the key, then published
    with ``os.link`` so an occupant is never replaced. If ``dest`` already holds
    bytes, a matching digest skips the GET; a differing digest is kept. Volumes
    that refuse hard links fall back to ``os.replace`` only when ``dest`` is
    absent, leaving a narrow TOCTOU window on that branch.
    """
    require_credentials(cfg)
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        outcome = _classify_existing_dest(target, sha256, cancelled=cancelled)
        return RestorePublish(outcome, 0)

    key = asset_object_key(sha256)
    temp = restore_temp_path(target)
    sha = hashlib.sha256()
    written = 0

    class _VerifyingSink:
        def __init__(self, handle: BinaryIO) -> None:
            self._handle = handle

        def write(self, chunk: bytes) -> int:
            nonlocal written
            if cancelled():
                raise OffloadCancelled(f"cancelled while restoring {target}")
            sha.update(chunk)
            written += len(chunk)
            return self._handle.write(chunk)

    try:
        with temp.open("wb") as handle:
            s3.download_to(cfg.audio_bucket, key, _VerifyingSink(handle), on_chunk)  # type: ignore[arg-type]
            handle.flush()
            os.fsync(handle.fileno())
        _require_digest(sha.hexdigest(), sha256, f"{key} -> {target}")
        return _publish_verified_temp(temp, target, sha256, written, cancelled=cancelled)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


# --- production adapter -----------------------------------------------------


def boto3_offload_client(cfg: CloudConfig) -> OffloadS3:  # pragma: no cover - network I/O
    """The asset adapter plus a chunked ``download_to`` over boto3."""
    from .asset_store import boto3_asset_client

    base = boto3_asset_client(cfg)
    import boto3  # type: ignore[import-not-found]

    from .asset_store import SIGV4_REGION

    client = boto3.client(
        "s3",
        endpoint_url=cfg.r2_endpoint,
        aws_access_key_id=cfg.r2_access_key_id,
        aws_secret_access_key=cfg.r2_secret_access_key,
        region_name=SIGV4_REGION,
    )

    class _Adapter:
        head_object = staticmethod(base.head_object)
        get_object = staticmethod(base.get_object)
        put_object_if_none_match = staticmethod(base.put_object_if_none_match)
        delete_object = staticmethod(base.delete_object)

        def download_to(
            self, bucket: str, key: str, handle: BinaryIO, on_chunk: ChunkProgress
        ) -> None:
            try:
                body = client.get_object(Bucket=bucket, Key=key)["Body"]
            except Exception as exc:
                raise AssetStoreError(f"GET {key} failed: {exc}") from exc
            for chunk in body.iter_chunks(HASH_CHUNK_BYTES):
                handle.write(chunk)
                on_chunk(len(chunk))

    return _Adapter()


__all__ = [
    "HASH_CHUNK_BYTES",
    "RESTORE_TEMP_INFIX",
    "LocalDigest",
    "OffloadCancelled",
    "OffloadS3",
    "OffloadVerifyError",
    "RestorePublish",
    "boto3_offload_client",
    "download_verified",
    "hash_local_file",
    "restore_temp_path",
    "unchanged_since_hash",
    "verify_remote_copy",
]
