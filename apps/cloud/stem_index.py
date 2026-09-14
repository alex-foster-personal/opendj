"""Stem bundle hydration index: ``stable_id -> {filename: sha256}`` (ADR-0024).

THE GAP THIS CLOSES: ``scripts/local_stems_to_r2.py`` content-addresses every
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
added, so :func:`upsert_object` performs an explicit create-or-replace
instead of the asset tier's conditional create.

* [if] the journal has a (legacy_key, key) pair for a stem object [then]
  the index maps that object's stable_id + filename to its sha256.
* [if] the journal's final line is torn (a kill mid-append) [then] the
  index still builds from every earlier, well-formed line.
* [if] the R2 index object does not exist yet [then] :func:`upsert_object`
  creates it; [if] it already exists [then] the same call replaces its body.
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
from pathlib import Path
from typing import TypeAlias

from apps.cloud.config import CloudConfig
from apps.cloud.lock import S3Client
from apps.cloud.r2_keys import R2KeyError, parse_legacy_stem_key

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
    (``scripts/local_stems_to_r2.py._truncate_torn_tail`` /
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
    return payload["stable_ids"]


def upsert_object(s3: S3Client, bucket: str, key: str, body: bytes) -> str:
    """Create-or-replace ``key``. The one deliberate non-content-addressed
    write in this codebase's R2 usage; see the module docstring.

    ``put_object_if_none_match`` is content-addressed R2's normal write and
    treats "already exists" as success without touching the body. A pointer
    object needs the opposite: the second and every later publish MUST
    replace the body, so a failed create falls through to an unconditional
    ``If-Match: *`` overwrite.
    """
    created, etag = s3.put_object_if_none_match(bucket, key, body)
    if created:
        if etag is None:
            raise StemIndexError(f"put_object_if_none_match({key}) created but returned no etag")
        return etag
    ok, etag2 = s3.put_object_if_match(bucket, key, body, etag="*")
    if not ok or etag2 is None:
        raise StemIndexError(f"failed to overwrite index object {bucket}/{key}")
    return etag2


def publish_index(cfg: CloudConfig, s3: S3Client, index: StemAssetIndex) -> str:
    """PUT the whole index to its fixed R2 key. Returns the resulting etag."""
    return upsert_object(s3, cfg.audio_bucket, INDEX_OBJECT_KEY, _encode(index))


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


def save_cached_index(data_dir: Path, index: StemAssetIndex) -> Path:
    path = local_index_cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_encode(index) + b"\n")
    return path


def refresh_local_cache_from_r2(cfg: CloudConfig, s3: S3Client, data_dir: Path) -> StemAssetIndex:
    """Fetch the R2 index and persist it as the local cache. Explicit,
    network-touching; never called from a request-serving code path."""
    index = fetch_index(cfg, s3)
    save_cached_index(data_dir, index)
    return index


__all__ = [
    "INDEX_CACHE_FILENAME",
    "INDEX_OBJECT_KEY",
    "INDEX_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "StemAssetIndex",
    "StemFileHashes",
    "StemIndexError",
    "build_index_from_journal",
    "fetch_index",
    "load_cached_index",
    "local_index_cache_path",
    "publish_index",
    "refresh_local_cache_from_r2",
    "save_cached_index",
    "upsert_object",
]
