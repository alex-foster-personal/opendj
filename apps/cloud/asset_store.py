"""Content-addressed asset tier on R2 (ADR 06, ``specs/design_decision_06.md``).

R2 is the CANONICAL home of every large derived artifact: stem bundles, HQ
audio copies, listening evidence. Objects are content-addressed::

    assets/<sha256[:2]>/<sha256>

Content addressing makes every object immutable: the key IS the digest of the
body, so "already uploaded" and "identical bytes" are the same question. That
is why the write path uses a plain conditional create
(:meth:`AssetS3Client.put_object_if_none_match`) and never a CAS overwrite --
there is no such thing as a newer version of a given key.

Two client surfaces, both narrow, both deliberate:

* :class:`AssetS3Client` -- the injected S3 protocol, extending the four
  methods of :class:`apps.cloud.lock.S3Client` with ``head_object`` (existence
  without pulling a multi-GB body) and an unconditional ``delete_object``
  (an immutable key has no etag to race against). Tests inject a dict-backed
  fake; production injects :func:`boto3_asset_client`.
* :func:`presign_url` -- pure SigV4 query signing over stdlib ``hmac`` /
  ``hashlib``. No boto3 import, so the read path works in the base venv
  (boto3 is an opt-in extra here) and the signature is deterministic enough
  to assert on. Expiry is capped at :data:`MAX_PRESIGN_EXPIRY_SECONDS`.

Credentials always come from Doppler. Every entry point calls
:func:`require_credentials` first, so a missing secret raises a
:class:`apps.cloud.config.MissingEnvError` naming the exact variable rather
than surfacing as an opaque 403 from Cloudflare later.
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal, Protocol
from urllib.parse import quote

from .config import REQUIRED_VARS, CloudConfig, MissingEnvError

ASSET_PREFIX: str = "assets"
HASH_SHARD_LEN: int = 2
SHA256_HEX_LEN: int = 64

#: R2/S3 refuse a presigned URL that outlives seven days; ADR 06 point 4 says
#: "short expiry, minted lazily", and the lane contract pins the ceiling here.
MAX_PRESIGN_EXPIRY_SECONDS: int = 900
DEFAULT_PRESIGN_EXPIRY_SECONDS: int = MAX_PRESIGN_EXPIRY_SECONDS

SIGV4_ALGORITHM: str = "AWS4-HMAC-SHA256"
SIGV4_REGION: str = "auto"  # R2 has one region and it is spelled "auto".
SIGV4_SERVICE: str = "s3"
UNSIGNED_PAYLOAD: str = "UNSIGNED-PAYLOAD"

HttpMethod = Literal["GET", "PUT", "HEAD", "DELETE"]
AssetProgress = Callable[[int, int], None]
AssetBody = bytes | BinaryIO

_HASH_CHUNK_BYTES: int = 1 << 20


class AssetStoreError(RuntimeError):
    """Raised when an asset-tier operation cannot be completed."""


# --- S3 protocol ---------------------------------------------------------


@dataclass(frozen=True)
class AssetHead:
    """What ``HEAD <object>`` tells us: size and etag, nothing more."""

    size: int
    etag: str


class AssetS3Client(Protocol):
    """Narrow S3 surface the asset tier needs.

    ``get_object`` and ``put_object_if_none_match`` keep the exact shapes
    used by :class:`apps.cloud.lock.S3Client`, so one production adapter can
    satisfy both protocols.
    """

    def head_object(self, bucket: str, key: str) -> AssetHead | None:
        """Return size + etag, or ``None`` when the key does not exist."""

    def get_object(self, bucket: str, key: str) -> tuple[bytes, str] | None:
        """Return ``(body, etag)`` or ``None`` if the object does not exist."""

    def put_object_if_none_match(
        self, bucket: str, key: str, body: AssetBody
    ) -> tuple[bool, str | None]:
        """Create if absent. Returns ``(created, new_etag)``.

        ``created=False`` means the key already existed. For a
        content-addressed key that is success, not conflict.
        """

    def delete_object(self, bucket: str, key: str) -> bool:
        """Delete unconditionally. ``True`` if an object was removed."""


# --- keys + hashing ------------------------------------------------------


def validate_content_hash(content_hash: str) -> str:
    """Return ``content_hash`` if it is a lowercase hex SHA-256, else raise.

    Guessing here would mint a key that no producer can ever reproduce, so
    an uppercase or truncated digest is a hard error rather than a coerced
    value.
    """
    candidate = content_hash.strip()
    if len(candidate) != SHA256_HEX_LEN or any(
        c not in "0123456789abcdef" for c in candidate
    ):
        raise AssetStoreError(
            f"content_hash must be {SHA256_HEX_LEN} lowercase hex chars "
            f"(a SHA-256 digest); got {content_hash!r}"
        )
    return candidate


def asset_object_key(content_hash: str) -> str:
    """Return ``assets/<hash[:2]>/<hash>`` for a validated digest.

    The two-char shard keeps any single R2 listing prefix at roughly 1/256
    of the bucket, which matters once stem bundles run to five figures.
    """
    digest = validate_content_hash(content_hash)
    return f"{ASSET_PREFIX}/{digest[:HASH_SHARD_LEN]}/{digest}"


def compute_asset_hash(path: Path, chunk_size: int = _HASH_CHUNK_BYTES) -> str:
    """Stream-hash the file at ``path`` and return its hex SHA-256."""
    target = Path(path)
    if not target.is_file():
        raise AssetStoreError(f"asset missing or not a regular file: {target}")
    digest = hashlib.sha256()
    with open(target, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- credentials ---------------------------------------------------------


def require_credentials(cfg: CloudConfig) -> None:
    """Raise :class:`MissingEnvError` if any R2 credential field is empty.

    :meth:`CloudConfig.from_env` already guards the env-loading path, but a
    directly constructed config (tests, callers stitching a config together)
    can still carry blanks. Failing here names the Doppler secret; failing
    later names nothing but a 403.
    """
    present = {
        "R2_ACCOUNT_ID": cfg.r2_account_id,
        "R2_ACCESS_KEY_ID": cfg.r2_access_key_id,
        "R2_SECRET_ACCESS_KEY": cfg.r2_secret_access_key,
    }
    missing = [name for name in REQUIRED_VARS if not present[name].strip()]
    if missing:
        raise MissingEnvError(
            "Missing R2 credentials: "
            + ", ".join(missing)
            + ". They live in Doppler (project general, config dev_personal); "
            "invoke under `doppler run -p general -c dev_personal -- ...`."
        )


# --- object operations ---------------------------------------------------


@dataclass(frozen=True)
class PushResult:
    """Outcome of :func:`push_asset`."""

    content_hash: str
    object_key: str
    bucket: str
    size_bytes: int
    #: ``False`` when the digest was already in the bucket. Not a failure:
    #: an identical key holds identical bytes by construction.
    uploaded: bool


class _ProgressReader:
    """File-like request body that reports bytes as the S3 client reads them."""

    def __init__(self, handle: BinaryIO, total: int, callback: AssetProgress) -> None:
        self._handle = handle
        self._total = total
        self._callback = callback
        self.bytes_transferred = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        if chunk:
            # Retries may seek backwards and re-read bytes. Publish the
            # high-water position, not cumulative reads, so progress never
            # exceeds total or regresses.
            self.bytes_transferred = max(self.bytes_transferred, self._handle.tell())
            self._callback(self.bytes_transferred, self._total)
        return chunk

    def tell(self) -> int:
        return self._handle.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._handle.seek(offset, whence)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True


def object_exists(cfg: CloudConfig, s3: AssetS3Client, content_hash: str) -> bool:
    """True iff the content-addressed object is present in the audio bucket."""
    require_credentials(cfg)
    key = asset_object_key(content_hash)
    return s3.head_object(cfg.audio_bucket, key) is not None


def push_asset(
    cfg: CloudConfig,
    s3: AssetS3Client,
    path: Path,
    *,
    on_progress: AssetProgress | None = None,
) -> PushResult:
    """Hash ``path``, upload it if absent, and return the resulting key.

    Idempotent by construction: a second call for identical bytes performs a
    HEAD, finds the key, and returns ``uploaded=False`` without re-sending
    the body.
    """
    require_credentials(cfg)
    source = Path(path)
    content_hash = compute_asset_hash(source)
    key = asset_object_key(content_hash)
    size_bytes = source.stat().st_size

    head = s3.head_object(cfg.audio_bucket, key)
    if head is not None:
        return PushResult(
            content_hash=content_hash,
            object_key=key,
            bucket=cfg.audio_bucket,
            size_bytes=head.size,
            uploaded=False,
        )

    if on_progress is None:
        created, _etag = s3.put_object_if_none_match(
            cfg.audio_bucket, key, source.read_bytes()
        )
    else:
        on_progress(0, size_bytes)
        with source.open("rb") as handle:
            progress_body = _ProgressReader(handle, size_bytes, on_progress)
            created, _etag = s3.put_object_if_none_match(
                cfg.audio_bucket, key, progress_body
            )
        if created and progress_body.bytes_transferred != size_bytes:
            raise AssetStoreError(
                f"upload client accepted {key} after consuming "
                f"{progress_body.bytes_transferred}/{size_bytes} bytes"
            )
    if not created and s3.head_object(cfg.audio_bucket, key) is None:
        # The conditional create was rejected AND the key is still absent.
        # Something other than a benign concurrent upload went wrong; do not
        # pretend the asset is durable.
        raise AssetStoreError(
            f"upload of {source} to {cfg.audio_bucket}/{key} was rejected and "
            "the key is still absent"
        )
    return PushResult(
        content_hash=content_hash,
        object_key=key,
        bucket=cfg.audio_bucket,
        size_bytes=size_bytes,
        uploaded=created,
    )


def fetch_asset(
    cfg: CloudConfig, s3: AssetS3Client, content_hash: str, dest: Path
) -> Path:
    """Download the object for ``content_hash`` to ``dest`` and verify it.

    The digest of the retrieved bytes is re-checked against the key before
    ``dest`` is written, so a truncated or mismatched download can never be
    mistaken for a cache hit later.
    """
    require_credentials(cfg)
    digest = validate_content_hash(content_hash)
    key = asset_object_key(digest)
    got = s3.get_object(cfg.audio_bucket, key)
    if got is None:
        raise AssetStoreError(
            f"asset {digest} is not in {cfg.audio_bucket} (key {key})"
        )
    body, _etag = got
    actual = hashlib.sha256(body).hexdigest()
    if actual != digest:
        raise AssetStoreError(
            f"content mismatch fetching {key}: body hashes to {actual}"
        )
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def delete_asset(cfg: CloudConfig, s3: AssetS3Client, content_hash: str) -> bool:
    """Remove the object for ``content_hash``. ``True`` if one was removed."""
    require_credentials(cfg)
    return s3.delete_object(cfg.audio_bucket, asset_object_key(content_hash))


# --- presigning (SigV4 query auth, stdlib only) --------------------------


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str) -> bytes:
    key = _sign(("AWS4" + secret).encode("utf-8"), datestamp)
    key = _sign(key, region)
    key = _sign(key, SIGV4_SERVICE)
    return _sign(key, "aws4_request")


def sigv4_presigned_url(
    *,
    method: str,
    host: str,
    canonical_uri: str,
    access_key_id: str,
    secret_access_key: str,
    expiry_seconds: int,
    amz_date: str,
    region: str = SIGV4_REGION,
) -> str:
    """Return a SigV4 query-authenticated URL. Pure function, no I/O.

    Kept generic in ``region`` and ``host`` for one reason: it lets the test
    suite drive AWS's own published presigned-URL example through THIS code
    and compare against AWS's published signature. A signer verified only
    against itself is self-consistently wrong for free, passes every unit
    test, and 403s the first time it meets Cloudflare. (``service`` is not a
    parameter: R2 and the AWS S3 vector are both ``s3``.)

    ``canonical_uri`` must already be percent-encoded with ``/`` preserved.
    """
    datestamp = amz_date[:8]
    scope = f"{datestamp}/{region}/{SIGV4_SERVICE}/aws4_request"
    params = {
        "X-Amz-Algorithm": SIGV4_ALGORITHM,
        "X-Amz-Credential": f"{access_key_id}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expiry_seconds),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(
        f"{quote(name, safe='')}={quote(params[name], safe='')}"
        for name in sorted(params)
    )
    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            canonical_query,
            f"host:{host}\n",
            "host",
            UNSIGNED_PAYLOAD,
        ]
    )
    string_to_sign = "\n".join(
        [
            SIGV4_ALGORITHM,
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_access_key, datestamp, region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        f"https://{host}{canonical_uri}?{canonical_query}"
        f"&X-Amz-Signature={signature}"
    )


def presign_url(
    cfg: CloudConfig,
    content_hash: str,
    expiry_seconds: int = DEFAULT_PRESIGN_EXPIRY_SECONDS,
    *,
    method: HttpMethod = "GET",
    now: datetime | None = None,
) -> str:
    """Return a short-lived signed URL for the content-addressed object.

    ``expiry_seconds`` is bounded to ``1 .. 900``. ADR 06 point 4 requires a
    short expiry minted lazily, and the hydration cache is keyed by
    ``content_hash`` NEVER by URL precisely because every mint differs.

    ``method`` covers the live round-trip (PUT, ranged GET, DELETE) without
    dragging boto3 into the base venv. ``now`` exists so the signature is
    reproducible in a test.
    """
    require_credentials(cfg)
    if not 0 < expiry_seconds <= MAX_PRESIGN_EXPIRY_SECONDS:
        raise AssetStoreError(
            f"expiry_seconds must be in 1..{MAX_PRESIGN_EXPIRY_SECONDS}; "
            f"got {expiry_seconds}"
        )
    key = asset_object_key(content_hash)
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    return sigv4_presigned_url(
        method=method,
        host=f"{cfg.r2_account_id}.r2.cloudflarestorage.com",
        # R2 is path-style: the bucket is the first path segment.
        canonical_uri="/" + quote(f"{cfg.audio_bucket}/{key}", safe="/"),
        access_key_id=cfg.r2_access_key_id,
        secret_access_key=cfg.r2_secret_access_key,
        expiry_seconds=expiry_seconds,
        amz_date=stamp.strftime("%Y%m%dT%H%M%SZ"),
    )


# --- production adapter --------------------------------------------------


def boto3_asset_client(cfg: CloudConfig) -> AssetS3Client:  # pragma: no cover
    """Build an :class:`AssetS3Client` over boto3.

    boto3 is an opt-in extra in this repo (see ``pyproject.toml``), so the
    import is lazy and its absence is a named error rather than a collection
    failure for every caller of this module. Mirrors
    :func:`apps.cloud.replicate.boto3_s3_client`; not unit-tested because it
    is pure network I/O.
    """
    require_credentials(cfg)
    try:
        import boto3  # type: ignore[import-not-found]
        from botocore.exceptions import ClientError  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AssetStoreError(
            "boto3 is required for the R2 asset tier; install the cloud "
            "extra (`uv sync --extra cloud`)."
        ) from exc

    client = boto3.client(
        "s3",
        endpoint_url=cfg.r2_endpoint,
        aws_access_key_id=cfg.r2_access_key_id,
        aws_secret_access_key=cfg.r2_secret_access_key,
        region_name=SIGV4_REGION,
    )

    class _Boto3AssetAdapter:
        def head_object(self, bucket: str, key: str) -> AssetHead | None:
            try:
                resp = client.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("NoSuchKey", "NotFound", "404"):
                    return None
                raise
            return AssetHead(size=int(resp["ContentLength"]), etag=resp["ETag"])

        def get_object(self, bucket: str, key: str) -> tuple[bytes, str] | None:
            try:
                resp = client.get_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("NoSuchKey", "404"):
                    return None
                raise
            return resp["Body"].read(), resp["ETag"]

        def put_object_if_none_match(
            self, bucket: str, key: str, body: AssetBody
        ) -> tuple[bool, str | None]:
            try:
                resp = client.put_object(
                    Bucket=bucket, Key=key, Body=body, IfNoneMatch="*"
                )
                return True, resp["ETag"]
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("PreconditionFailed", "412"):
                    return False, None
                raise

        def delete_object(self, bucket: str, key: str) -> bool:
            client.delete_object(Bucket=bucket, Key=key)
            return True

    return _Boto3AssetAdapter()


__all__ = [
    "ASSET_PREFIX",
    "DEFAULT_PRESIGN_EXPIRY_SECONDS",
    "MAX_PRESIGN_EXPIRY_SECONDS",
    "AssetHead",
    "AssetS3Client",
    "AssetStoreError",
    "PushResult",
    "asset_object_key",
    "boto3_asset_client",
    "compute_asset_hash",
    "delete_asset",
    "fetch_asset",
    "object_exists",
    "presign_url",
    "push_asset",
    "require_credentials",
    "sigv4_presigned_url",
    "validate_content_hash",
]
