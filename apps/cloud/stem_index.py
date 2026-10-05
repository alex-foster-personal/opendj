"""Stem bundle hydration index: ``stable_id -> {filename: sha256}`` (ADR-0024).

THE GAP THIS CLOSES: ``apps.stems.r2_migration`` content-addresses every
pushed stem file and journals the (legacy_key, key) mapping to
``data/state/stem-r2-migration.jsonl`` -- but that journal lives only on the
machine that ran the push. A machine with no local bundle (silver, or any
future machine) has no way to learn which R2 object holds which part of
which track's stems. This module builds that mapping from the journal and
makes it reachable from any machine via one well-known R2 object.

WHY A NEW RECORD, NOT ``track_locations``: ``track_locations`` is shaped for
ONE file per (stable_id, kind, role) -- see ``apps/cloud/hydration_core.py``
``_local_file`` / ``_content_hash``, which both read a single row per track.
A stem bundle is 4-5 files (manifest + parts) that must all resolve before a
bundle can even be attempted, so it needs a multi-file mapping that
``track_locations`` cannot express without a schema change to a table many
other readers depend on. ADR-0024 records the decision to add this narrow,
purpose-built index instead of widening ``track_locations``.

WHY R2, NOT A NEW state.db TABLE: wiring a new table into the CloudSync
sync_hub protocol (``SYNC_TABLES``, migrations, wire_version, LWW changelog
triggers) is real, ongoing, cross-cutting work with its own review surface;
see ``apps/sync_hub/protocol_common.py``. R2 is already the canonical,
every-machine-reachable store for stem bytes (ADR 06), so a small JSON
object at a FIXED (not content-addressed) key is the smallest correct
extension of the SAME store, not a new subsystem. This is a deliberate
exception to ADR 06's content-addressing rule: every other R2 object here is
immutable, but the index is a pointer that must be overwritten as bundles are
added, so :func:`publish_index` merges into the shared object under
compare-and-swap (read current + etag, union by stable_id with the
caller's entries winning, write with If-Match / If-None-Match, bounded
retry) instead of the asset tier's conditional create or a blind overwrite.

* [if] the journal has a (legacy_key, key) pair for a stem object [then]
  the index maps that object's stable_id + filename to its sha256.
* [if] the journal's final line is torn (a kill mid-append) [then] the
  index still builds from every earlier, well-formed line.
* [if] the R2 index object does not exist yet [then] :func:`publish_index`
  creates it; [if] it already exists [then] the same call merges the
  caller's entries into the current body under CAS.
* [if] no index object exists in R2 [then] :func:`fetch_index` returns an
  empty index, not an error -- a fleet with no stem push run yet has
  nothing to hydrate from, which is a fact, not a failure.
* [if] the local cache file is missing [then] :func:`load_cached_index`
  returns an empty index; [if] the file exists but is corrupt or the wrong
  shape [then] it raises :class:`StemIndexError` rather than silently
  reading as empty.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeAlias

from apps.cloud.asset_store import AssetStoreError, validate_content_hash
from apps.cloud.config import CloudConfig
from apps.cloud.lock import S3Client
from apps.cloud.r2_keys import R2KeyError, parse_legacy_stem_key
from apps.stems.artifacts import ROFORMER_PARTS, STEM_PARTS

#: A fixed, mutable pointer key. NOT content-addressed: unlike every other
#: object under ``assets/``, this one is replaced in place as new bundles
#: are pushed, so its key cannot be derived from its own body.
INDEX_OBJECT_KEY: str = "indexes/stem-bundle-index.json"
INDEX_SCHEMA_VERSION: int = 1
INDEX_CACHE_FILENAME: str = "stem-bundle-index.json"

#: filename -> sha256 hex, for one stable_id's manifest + parts.
StemFileHashes: TypeAlias = dict[str, str]
#: stable_id -> StemFileHashes.
StemAssetIndex: TypeAlias = dict[str, StemFileHashes]

MANIFEST_FILENAME: str = "manifest.json"

#: Must stay in sync with ``stem_artifacts._MEDIA_TYPES`` keys.
STEM_MEDIA_EXTENSIONS: tuple[str, str, str] = (".wav", ".flac", ".mp3")

_ALLOWED_PARTS: tuple[str, ...] = tuple(dict.fromkeys((*STEM_PARTS, *ROFORMER_PARTS)))
ALLOWED_STEM_FILENAMES: frozenset[str] = frozenset(
    {MANIFEST_FILENAME}
    | {
        f"{part}{ext}"
        for part in _ALLOWED_PARTS
        for ext in STEM_MEDIA_EXTENSIONS
    }
)

_PUBLISH_CAS_MAX_ATTEMPTS: int = 5


def is_allowed_stem_filename(filename: str) -> bool:
    """Return whether ``filename`` is an exact allowed bundle basename."""
    return filename in ALLOWED_STEM_FILENAMES


class StemIndexError(RuntimeError):
    """Raised when the stem bundle index cannot be built, read or published."""


# --- building from the push-rail journal ------------------------------------


def _sha256_from_asset_key(key: str) -> str | None:
    """Return the digest a content-addressed ``assets/<xx>/<sha256>`` key
    names, or ``None`` if ``key`` is not that shape (never raises: a journal
    line naming an object from a different rail is skipped, not fatal)."""
    parts = key.split("/")
    if len(parts) != 3 or parts[0] != "assets" or parts[1] != parts[2][:2]:
        return None
    digest = parts[2]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return None
    return digest


def build_index_from_journal(journal_path: Path) -> StemAssetIndex:
    """Fold every journaled (legacy_key, key) mapping into an index.

    Later journal lines win for the same (stable_id, filename): the journal
    is append-only and chronological, so a re-rendered bundle's newer mapping
    correctly shadows an older one for the same file name. A missing journal
    is an empty index, not an error -- the push lane may not have run yet on
    this machine.

    Tolerates a torn FINAL line exactly like the journal's own writer/reader
    (``apps.stems.r2_migration._truncate_torn_tail`` /
    ``_journaled_mappings``): a process killed mid-append leaves a truncated
    last record, and that must not block reading everything recorded before
    it. An earlier malformed line is real corruption and still raises.
    """
    index: StemAssetIndex = {}
    path = Path(journal_path)
    if not path.is_file():
        return index
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for position, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if position == len(lines) - 1:
                break
            raise StemIndexError(
                f"{journal_path} line {position + 1} is not valid JSON and is "
                "not the final line; refusing to build a partial index from a "
                "corrupt journal"
            ) from None
        for obj in record.get("objects", []):
            legacy_key = obj.get("legacy_key", "")
            key = obj.get("key", "")
            try:
                parsed = parse_legacy_stem_key(legacy_key)
            except R2KeyError:
                continue  # not a stem object; another rail may share this journal shape one day
            digest = _sha256_from_asset_key(key)
            if digest is None:
                continue
            index.setdefault(parsed.stable_id, {})[parsed.filename] = digest
    return index


# --- publishing / fetching the R2 pointer object -----------------------------


def _encode(index: StemAssetIndex) -> bytes:
    return json.dumps(
        {"schema_version": INDEX_SCHEMA_VERSION, "stable_ids": index}, sort_keys=True
    ).encode("utf-8")


def _decode(body: bytes) -> StemAssetIndex:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise StemIndexError(
            f"stem bundle index at {INDEX_OBJECT_KEY} is not valid JSON"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("stable_ids"), dict):
        raise StemIndexError(
            f"stem bundle index at {INDEX_OBJECT_KEY} is missing a 'stable_ids' object"
        )
    stable_ids = payload["stable_ids"]
    for stable_id, file_hashes in stable_ids.items():
        if not isinstance(file_hashes, dict):
            raise StemIndexError(
                f"stem bundle index at {INDEX_OBJECT_KEY} has a non-object entry "
                f"for stable_id {stable_id!r}"
            )
        for filename, digest in file_hashes.items():
            if not is_allowed_stem_filename(filename):
                raise StemIndexError(
                    f"stem bundle index at {INDEX_OBJECT_KEY} has disallowed "
                    f"filename {filename!r} for stable_id {stable_id!r}"
                )
            if not isinstance(digest, str):
                raise StemIndexError(
                    f"stem bundle index at {INDEX_OBJECT_KEY} has a non-string "
                    f"digest for stable_id {stable_id!r} file {filename!r}"
                )
            try:
                validate_content_hash(digest)
            except AssetStoreError as exc:
                raise StemIndexError(
                    f"stem bundle index at {INDEX_OBJECT_KEY} has invalid digest "
                    f"for stable_id {stable_id!r} file {filename!r}: {exc}"
                ) from exc
    return stable_ids


def publish_index(cfg: CloudConfig, s3: S3Client, index: StemAssetIndex) -> str:
    """Merge ``index`` into the shared R2 pointer object under CAS.

    Reads the current object (if any), unions by ``stable_id`` with the
    caller's entries winning on collision, and writes with
    ``If-None-Match`` (create) or ``If-Match`` (update). Retries up to
    :data:`_PUBLISH_CAS_MAX_ATTEMPTS` on precondition failure. Returns the
    resulting etag.
    """
    bucket = cfg.audio_bucket
    for _attempt in range(1, _PUBLISH_CAS_MAX_ATTEMPTS + 1):
        got = s3.get_object(bucket, INDEX_OBJECT_KEY)
        current = _decode(got[0]) if got is not None else {}
        current_etag = got[1] if got is not None else None
        merged = dict(current)
        merged.update(index)
        body = _encode(merged)
        if current_etag is None:
            created, new_etag = s3.put_object_if_none_match(bucket, INDEX_OBJECT_KEY, body)
            if created:
                if new_etag is None:
                    raise StemIndexError(
                        f"put_object_if_none_match({INDEX_OBJECT_KEY}) created but "
                        "returned no etag"
                    )
                return new_etag
        else:
            ok, new_etag = s3.put_object_if_match(
                bucket, INDEX_OBJECT_KEY, body, etag=current_etag
            )
            if ok:
                if new_etag is None:
                    raise StemIndexError(
                        f"put_object_if_match({INDEX_OBJECT_KEY}) succeeded but "
                        "returned no etag"
                    )
                return new_etag
    raise StemIndexError(
        f"failed to publish {INDEX_OBJECT_KEY} after {_PUBLISH_CAS_MAX_ATTEMPTS} "
        "compare-and-swap attempts"
    )


def fetch_index(cfg: CloudConfig, s3: S3Client) -> StemAssetIndex:
    """GET the index from R2. An absent object is an empty index, not an error."""
    got = s3.get_object(cfg.audio_bucket, INDEX_OBJECT_KEY)
    if got is None:
        return {}
    body, _etag = got
    return _decode(body)


# --- local cache --------------------------------------------------------------


def local_index_cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / INDEX_CACHE_FILENAME


def load_cached_index(data_dir: Path) -> StemAssetIndex:
    """Read the local cache copy.

    Missing file = empty index (cold cache, nothing to hydrate yet).
    Present but unreadable = :class:`StemIndexError` (real corruption, must
    not be silently treated as "nothing indexed").
    """
    path = local_index_cache_path(data_dir)
    if not path.is_file():
        return {}
    try:
        return _decode(path.read_bytes())
    except StemIndexError as exc:
        raise StemIndexError(f"{path} is unreadable: {exc}") from exc


_memo_lock = threading.Lock()
_memo: dict[Path, tuple[tuple[int, int] | None, StemAssetIndex]] = {}


def load_cached_index_memo(data_dir: Path) -> StemAssetIndex:
    """:func:`load_cached_index`, re-parsed only when the cache file changes.

    For readers that run on every poll (the coverage snapshot): decoding and
    validating about 1600 bundles costs 24 ms, a stat costs microseconds. The
    key is the file's (mtime_ns, size); an absent file is keyed ``None`` and
    reads as empty, exactly like the uncached loader, and a corrupt file
    raises every time (a failure is never memoized). The returned mapping is
    SHARED: callers must not mutate it.
    """
    path = local_index_cache_path(data_dir)
    try:
        stat = path.stat()
        signature: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        signature = None
    with _memo_lock:
        cached = _memo.get(path)
        if cached is not None and cached[0] == signature:
            return cached[1]
    index = load_cached_index(data_dir)
    with _memo_lock:
        _memo[path] = (signature, index)
    return index


def save_cached_index(data_dir: Path, index: StemAssetIndex) -> Path:
    validated = _decode(_encode(index))
    path = local_index_cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_encode(validated) + b"\n")
    return path


def refresh_local_cache_from_r2(cfg: CloudConfig, s3: S3Client, data_dir: Path) -> StemAssetIndex:
    """Fetch the R2 index and persist it as the local cache. Explicit,
    network-touching; not called directly from request-serving code paths --
    use :func:`refresh_local_cache_from_r2_throttled` there instead."""
    index = fetch_index(cfg, s3)
    save_cached_index(data_dir, index)
    return index


#: Floor between REQUEST-PATH refresh attempts for the same data_dir, so a
#: cold cache with nothing published (or a flaky network) cannot turn every
#: manifest miss into a fresh R2 round trip. Named constant, not a magic
#: number, per this repo's brittle-fail-fast convention.
INDEX_REFRESH_RETRY_INTERVAL_S: float = 300.0

_refresh_lock = threading.Lock()
_last_refresh_attempt_mono: dict[Path, float] = {}
_last_refresh_error: dict[Path, str] = {}


def refresh_error(data_dir: Path) -> str | None:
    """The most recent index refresh failure recorded for this data_dir, or
    ``None`` if the last attempt (if any) succeeded or none has run yet."""
    return _last_refresh_error.get(Path(data_dir))


def refresh_local_cache_throttled(
    data_dir: Path,
    fetch: Callable[[], StemAssetIndex],
    *,
    force: bool = False,
) -> bool:
    """Backoff-guarded index refresh using ``fetch`` as the network source."""
    data_dir = Path(data_dir)
    now = time.monotonic()
    with _refresh_lock:
        last = _last_refresh_attempt_mono.get(data_dir)
        if not force and last is not None and now - last < INDEX_REFRESH_RETRY_INTERVAL_S:
            return False
        _last_refresh_attempt_mono[data_dir] = now
    try:
        index = fetch()
        save_cached_index(data_dir, index)
    except Exception as exc:
        _last_refresh_error[data_dir] = str(exc)
        raise
    _last_refresh_error.pop(data_dir, None)
    return True


def refresh_local_cache_from_r2_throttled(
    cfg: CloudConfig, s3: S3Client, data_dir: Path, *, force: bool = False
) -> bool:
    """Single-flight, backoff-guarded wrapper around
    :func:`refresh_local_cache_from_r2`, safe to call from a request-serving
    code path (unlike that function, which stays explicit/CLI-only).

    Returns ``True`` if a refresh actually ran this call (success or
    failure -- check :func:`refresh_error` after), ``False`` if skipped
    because one already ran for this ``data_dir`` within
    :data:`INDEX_REFRESH_RETRY_INTERVAL_S`. ``force=True`` bypasses the
    backoff (used once, at process startup). At most one refresh per
    interval per data_dir: the attempt timestamp is stamped while holding
    the lock, before the network call, so a second caller arriving
    immediately after sees the stamp and skips rather than racing it.
    """
    return refresh_local_cache_throttled(
        data_dir,
        lambda: refresh_local_cache_from_r2(cfg, s3, data_dir),
        force=force,
    )


__all__ = [
    "ALLOWED_STEM_FILENAMES",
    "INDEX_CACHE_FILENAME",
    "INDEX_OBJECT_KEY",
    "INDEX_REFRESH_RETRY_INTERVAL_S",
    "INDEX_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "STEM_MEDIA_EXTENSIONS",
    "StemAssetIndex",
    "StemFileHashes",
    "StemIndexError",
    "build_index_from_journal",
    "fetch_index",
    "is_allowed_stem_filename",
    "load_cached_index",
    "load_cached_index_memo",
    "local_index_cache_path",
    "publish_index",
    "refresh_error",
    "refresh_local_cache_from_r2",
    "refresh_local_cache_from_r2_throttled",
    "refresh_local_cache_throttled",
    "save_cached_index",
]
