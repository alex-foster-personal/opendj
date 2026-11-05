"""The signed store index: what can be installed, and what must be disabled.

ONE INDEX PER CHANNEL, at ``<base>/<channel>.json`` with a detached
``<channel>.json.sig`` (:mod:`apps.plugins.signing`). The base is
:data:`CATALOG_BASE_URL`, the public ``opendj-store`` repository the proposal
on fleet-af #334 names; like the updater endpoint, it is a compiled-in
constant, and a different base only comes from ``MDT_PLUGIN_CATALOG_URL`` for
a lane or test. A different base cannot widen trust: the bytes must still be
signed by a compiled-in key.

The index pins each archive by sha256 and each manifest by its parsed
content, so an archive host can serve only what the signed index named
(PLUG-6522b). It also carries the signed ``blocklist`` the scanning doc asks
for: an installed plugin version on it is disabled at the next boot
(PLUG-6522f).

NO SILENT EMPTY STORE. A catalog that could not be fetched, verified or
parsed is a named fault carrying the URL and the reason. An empty list is
only ever reported for a verified index that lists nothing. Offline, the last
verified copy is served from the cache, re-verified on every read, and
labeled with when it was fetched.

Requirements (mini-PRD):
  * ✔︎ ✅ 🎯 an index whose signature fails is refused and NOT cached.
    [if] a tampered index reaches the cache [then ⛔️]
  * ✔︎ ✅ 🎯 an entry whose manifest names a different channel than the index
    is refused. [if] a labs entry can ride the stable index [then ⛔️]
  * ✔︎ ✅ 🎯 fetch failure with a verified cache serves the cache, marked
    stale, with the fault attached. [if] stale data reads as fresh [then ⛔️]
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from apps.plugins.listing import (
    MAX_SCREENSHOT_BYTES,
    Listing,
    ListingError,
    Screenshot,
    listing_from_dict,
    sniff_media_type,
)
from apps.plugins.manifest import (
    CHANNELS,
    ManifestError,
    PluginManifest,
    is_plugin_id,
    is_semver,
    is_sha256,
    manifest_from_dict,
)
from apps.plugins.regular import open_regular_file
from apps.plugins.signing import NO_TRUSTED_KEY_MESSAGE, TRUSTED_STORE_KEYS, SignatureError, verify
from apps.plugins.state import write_atomic

CATALOG_BASE_URL: str = "https://raw.githubusercontent.com/private_owner/opendj-store/main/index"
CATALOG_URL_ENV: str = "MDT_PLUGIN_CATALOG_URL"
INDEX_SCHEMA: int = 1
FETCH_TIMEOUT_S: float = 10.0
MAX_INDEX_BYTES: int = 4 * 1024 * 1024
#: A cache file holds one index and its signature, base64 (4/3 larger) in JSON;
#: anything bigger is not one this build wrote, so it is refused before reading.
MAX_CACHE_BYTES: int = 2 * MAX_INDEX_BYTES
#: A detached signature is a small JSON document (key id plus one signature); install receipts hold it to this too.
MAX_SIGNATURE_BYTES: int = 64 * 1024


class Fetcher(Protocol):
    """``fetch(url, max_bytes=...) -> bytes``; raises :class:`CatalogFetchError`."""

    def __call__(self, url: str, *, max_bytes: int = ...) -> bytes: ...


class CatalogError(ValueError):
    """An index that cannot be used. ``code`` is stable wire vocabulary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CatalogFetchError(CatalogError):
    pass


@dataclass(frozen=True)
class CatalogEntry:
    manifest: PluginManifest
    archive_url: str
    archive_sha256: str
    archive_size: int
    listing: Listing


@dataclass(frozen=True)
class BlockedVersion:
    plugin_id: str
    versions: tuple[str, ...]  # empty means every version
    reason: str

    def blocks(self, plugin_id: str, version: str) -> bool:
        return plugin_id == self.plugin_id and (not self.versions or version in self.versions)


@dataclass(frozen=True)
class Catalog:
    channel: str
    key_id: str
    generated_at: str
    entries: tuple[CatalogEntry, ...]
    blocklist: tuple[BlockedVersion, ...]

    @property
    def generated(self) -> datetime:
        """When the index was signed; parse_index only accepts a zoned ISO-8601 stamp."""
        return datetime.fromisoformat(self.generated_at)

    def find(self, plugin_id: str) -> CatalogEntry | None:
        for entry in self.entries:
            if entry.manifest.plugin_id == plugin_id:
                return entry
        return None


@dataclass(frozen=True)
class CatalogRead:
    """One channel's answer: a catalog, a fault, or a stale catalog plus a fault."""

    channel: str
    url: str
    catalog: Catalog | None
    fault: CatalogError | None = None
    fetched_at: float | None = None
    stale: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    #: The verified index bytes, kept as the install receipt (PLUG-6522f).
    payload: bytes = b""
    signature: bytes = b""


def catalog_base_url(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get(CATALOG_URL_ENV, CATALOG_BASE_URL).rstrip("/")


def _entry(raw: Any, channel: str) -> CatalogEntry:
    if not isinstance(raw, dict):
        raise CatalogError("bad-index", "an index entry must be an object")
    _known_keys(raw, _ENTRY_KEYS, "an index entry")
    try:
        manifest = manifest_from_dict(raw.get("manifest"))
    except ManifestError as exc:
        raise CatalogError("bad-index", f"entry manifest refused: {exc}") from exc
    if manifest.channel != channel:
        raise CatalogError(
            "bad-index",
            f"{manifest.plugin_id} is a {manifest.channel!r} plugin listed in the {channel!r} index",
        )
    url = raw.get("archive_url")
    digest = raw.get("archive_sha256")
    size = raw.get("archive_size")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise CatalogError("bad-index", f"{manifest.plugin_id}: archive_url must be https://")
    if not is_sha256(digest):  # a typo here signs an archive nothing can ever match
        raise CatalogError("bad-index", f"{manifest.plugin_id}: archive_sha256 must be lowercase hex sha256")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise CatalogError("bad-index", f"{manifest.plugin_id}: archive_size missing")
    return CatalogEntry(manifest, url, digest, size, _listing(raw, manifest, channel))


def _listing(raw: dict[str, Any], manifest: PluginManifest, channel: str) -> Listing:
    try:
        listing = listing_from_dict(raw.get("listing"), manifest.plugin_id)
    except ListingError as exc:
        raise CatalogError("bad-index", f"{manifest.plugin_id}: {exc}") from exc
    if listing.status == "experiment" and channel != "labs":
        raise CatalogError("bad-index", f"{manifest.plugin_id}: an experiment is listed only in labs")
    return listing


def _blocked(raw: Any) -> BlockedVersion:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        raise CatalogError("bad-index", "a blocklist row needs an 'id'")
    _known_keys(raw, _BLOCK_KEYS, "a blocklist row")
    versions = raw.get("versions", [])
    reason = raw.get("reason")
    if not isinstance(versions, list) or not all(isinstance(v, str) for v in versions):
        raise CatalogError("bad-index", "blocklist 'versions' must be a list of strings")
    # A typo here ("v1.0.0", "OpenDJ.x") would sign a revocation that matches nothing.
    if not is_plugin_id(raw["id"]):
        raise CatalogError("bad-index", f"blocklist id {raw['id']!r} is not a plugin id")
    bad = [v for v in versions if not is_semver(v)]
    if bad:
        raise CatalogError("bad-index", f"blocklist {raw['id']}: {bad!r} is not semver")
    if not isinstance(reason, str) or not reason.strip():
        raise CatalogError("bad-index", "a blocklist row needs a 'reason'")
    return BlockedVersion(raw["id"], tuple(versions), reason)


def _index_body(payload: bytes, channel: str) -> dict[str, Any]:
    """The verified bytes as a JSON object of this schema and channel."""
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError("bad-index", f"index is not UTF-8 JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != INDEX_SCHEMA:
        raise CatalogError("bad-index", f"index schema must be {INDEX_SCHEMA}")
    if raw.get("channel") != channel:
        raise CatalogError("bad-index", f"index says channel {raw.get('channel')!r}, fetched as {channel!r}")
    _known_keys(raw, _INDEX_KEYS, "the index")
    return raw


#: Every key a signed index, entry and blocklist row may carry. Anything else is
#: refused, so a misspelled "blocklist" cannot sign as no revocations at all.
_INDEX_KEYS = frozenset({"schema", "channel", "generated_at", "plugins", "blocklist"})
_ENTRY_KEYS = frozenset({"manifest", "archive_url", "archive_sha256", "archive_size", "listing"})
_BLOCK_KEYS = frozenset({"id", "versions", "reason"})


def _known_keys(raw: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise CatalogError("bad-index", f"{where} has unknown keys {unknown}")


def parse_index(
    payload: bytes,
    signature: bytes,
    channel: str,
    *,
    trusted: Mapping[str, str] | None = None,
) -> Catalog:
    """Verify, then parse. Verification first, so unsigned bytes are never parsed."""
    if channel not in CHANNELS:
        raise CatalogError("bad-channel", f"unknown channel {channel!r}")
    try:
        key_id = verify(payload, signature, trusted=trusted)
    except SignatureError as exc:
        raise CatalogError(exc.code, str(exc)) from exc
    raw = _index_body(payload, channel)
    plugins = raw.get("plugins")
    blocklist = raw.get("blocklist", [])
    if not isinstance(plugins, list) or not isinstance(blocklist, list):
        raise CatalogError("bad-index", "'plugins' and 'blocklist' must be lists")
    entries = tuple(_entry(item, channel) for item in plugins)
    ids = [e.manifest.plugin_id for e in entries]
    if len(set(ids)) != len(ids):
        raise CatalogError("bad-index", "the index lists one plugin id twice")
    return Catalog(
        channel=channel,
        key_id=key_id,
        generated_at=_generated_at(raw),
        entries=entries,
        blocklist=tuple(_blocked(item) for item in blocklist),
    )


def _generated_at(raw: Mapping[str, Any]) -> str:
    """The signing time: required, so a refresh can refuse an older, replayed index."""
    stamp = raw.get("generated_at")
    if not isinstance(stamp, str) or not _zoned(stamp):
        raise CatalogError("bad-index", f"'generated_at' must be an ISO-8601 time with a zone, not {stamp!r}")
    return stamp


def _zoned(stamp: str) -> bool:
    try:
        return datetime.fromisoformat(stamp).tzinfo is not None
    except ValueError:
        return False


def http_fetch(url: str, *, max_bytes: int = MAX_INDEX_BYTES) -> bytes:
    """The production fetcher: bounded, https only, every failure named."""
    import httpx

    if not url.startswith("https://"):
        raise CatalogFetchError("fetch-failed", f"refusing non-https catalog URL {url}")
    try:
        with (
            httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True) as client,
            client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response,
        ):
            if response.status_code != 200:
                raise CatalogFetchError("http-status", f"{url} answered HTTP {response.status_code}")
            encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
            if encoding not in {"", "identity"}:
                # A tiny compressed body can inflate past any cap before a decoded chunk is
                # seen, so an encoded body is refused and only raw bytes are ever read.
                raise CatalogFetchError(
                    "encoded-body", f"{url} sent a {encoding}-encoded body; only plain bodies are read"
                )
            return read_bounded(response.iter_raw(), url, max_bytes)
    except httpx.HTTPError as exc:
        raise CatalogFetchError("unreachable", f"{url}: {exc}") from exc


def read_bounded(chunks: Iterable[bytes], url: str, max_bytes: int) -> bytes:
    """Join a streamed body, stopping at the first chunk past ``max_bytes``.

    The host is not trusted until the signature checks, so an oversized or
    endless body is refused as it arrives rather than after it is buffered.
    """
    body = bytearray()
    for chunk in chunks:
        body += chunk
        if len(body) > max_bytes:
            raise CatalogFetchError("too-large", f"{url} is larger than {max_bytes} bytes")
    return bytes(body)


class CatalogCache:
    """Last verified index per channel, one file per channel under ``<plugins-dir>/catalog/``.

    Payload, signature and fetch time live in ONE file written in one atomic
    step, so a crash or an overlapping refresh can never pair one index with
    another's signature. A mismatched pair would fail verification at boot,
    and boot would then run without that channel's blocklist.
    """

    def __init__(self, plugins_dir: Path) -> None:
        self.root = plugins_dir / "catalog"

    def _path(self, channel: str) -> Path:
        return self.root / f"{channel}.cache.json"

    def _unlinked(self, channel: str) -> Path:
        """The cache file, refused when it or a folder above it is a link.

        A link written through would land outside the plugins folder; one read
        through could restore an older, still validly signed index from before a
        blocklist entry.
        """
        path = self._path(channel)
        if any(p.is_symlink() for p in (self.root.parent, self.root, path)):
            raise CatalogError("unsafe-cache", f"{self.root} is or holds a symlink; refusing to use it")
        return path

    def store(self, channel: str, payload: bytes, signature: bytes, now: float) -> None:
        self._unlinked(channel)
        body = {
            "fetched_at": now,
            "signature": base64.b64encode(signature).decode("ascii"),
            "payload": base64.b64encode(payload).decode("ascii"),
        }
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            write_atomic(self._path(channel), json.dumps(body).encode("utf-8"))
        except OSError as exc:  # a full or read-only disk, or a file where the folder goes: a fault, not a 500
            raise CatalogError("cache-write-failed", f"could not save the {channel} catalog: {exc}") from exc

    def _newest_path(self, channel: str) -> Path:
        """The replay-floor record, refused like the cache when it or a folder above it is a link.

        Read through a link, an older valid record would lower the floor and let
        a cache the newer index replaced stand in for it again.
        """
        path = self.root / f"{channel}.newest.json"
        if any(p.is_symlink() for p in (self.root.parent, self.root, path)):
            raise CatalogError("unsafe-cache", f"{path} is or sits under a symlink; refusing to use it")
        return path

    def newest(self, channel: str) -> tuple[datetime, str] | None:
        """The newest generation ever verified here (signing time, payload sha256), or None.

        Kept apart from the cache file and only ever raised, so losing or
        corrupting the cache cannot let an older signed index back in. None
        only when no record exists: a linked record raises ``unsafe-cache`` and
        an unreadable or malformed one ``bad-cache``, since reading either as
        no record would lower the floor (fail closed).
        """
        path = self._newest_path(channel)
        try:
            with open_regular_file(path) as handle:
                raw = json.loads(handle.read(4096))
            stamp = datetime.fromisoformat(raw["generated_at"])
            digest = raw["sha256"]
        except (FileNotFoundError, NotADirectoryError):  # no record, or no catalog folder to hold one
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CatalogError(
                "bad-cache", f"{path} is unreadable ({exc}); delete it to reset the replay floor"
            ) from exc
        if stamp.tzinfo is None or not is_sha256(digest):
            raise CatalogError("bad-cache", f"{path} is malformed; delete it to reset the replay floor")
        return stamp, digest

    def raise_newest(self, channel: str, generated_at: str, payload: bytes) -> None:
        self._unlinked(channel)
        current = self.newest(channel)
        if current is not None and current[0] >= datetime.fromisoformat(generated_at):
            return
        body = {"generated_at": generated_at, "sha256": hashlib.sha256(payload).hexdigest()}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            write_atomic(self._newest_path(channel), json.dumps(body).encode("utf-8"))
        except OSError as exc:
            raise CatalogError("cache-write-failed", f"could not record the newest {channel} catalog: {exc}") from exc

    def load(self, channel: str) -> tuple[bytes, bytes, float] | None:
        """The cached generation, or None. An unreadable cache raises a named CatalogError.

        The cache is read at boot, so a corrupt file must become a channel
        fault, never an exception that stops the engine starting.
        """
        path = self._unlinked(channel)
        if not path.exists():
            return None
        try:
            with open_regular_file(path) as handle:
                return _decode_cache(handle.read(MAX_CACHE_BYTES + 1))
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise CatalogError("bad-cache", f"cached {channel} catalog is unreadable: {exc}") from exc


def _decode_cache(data: bytes) -> tuple[bytes, bytes, float]:
    if len(data) > MAX_CACHE_BYTES:
        raise ValueError(f"larger than the {MAX_CACHE_BYTES}-byte limit for a cached index")
    raw = json.loads(data.decode("utf-8"))
    stamp = raw["fetched_at"]
    if not isinstance(stamp, int | float) or isinstance(stamp, bool) or not math.isfinite(stamp):
        raise TypeError(f"fetched_at is {stamp!r}")
    try:  # the page formats it as a date, so it must be one
        datetime.fromtimestamp(stamp, tz=UTC)
    except (OverflowError, OSError) as exc:
        raise ValueError(f"fetched_at {stamp!r} is not a representable time") from exc
    payload = base64.b64decode(raw["payload"], validate=True)
    signature = base64.b64decode(raw["signature"], validate=True)
    # The same caps a fetch applies: past them no receipt written from this cache could verify at boot.
    if len(payload) > MAX_INDEX_BYTES or len(signature) > MAX_SIGNATURE_BYTES:
        raise ValueError("cached index or signature is larger than any signed one a fetch accepts")
    return payload, signature, float(stamp)


_CHANNEL_LOCKS: dict[Path, threading.Lock] = {}
_CHANNEL_LOCKS_GUARD = threading.Lock()


def _channel_lock(cache: CatalogCache, channel: str) -> threading.Lock:
    """One lock per cache file, shared by every CatalogCache pointing at it."""
    key = cache._path(channel).resolve()
    with _CHANNEL_LOCKS_GUARD:
        return _CHANNEL_LOCKS.setdefault(key, threading.Lock())


def _replayed(
    channel: str, cache: CatalogCache, fetched: Catalog, payload: bytes, trusted: Mapping[str, str] | None
) -> CatalogError | None:
    """A ``rollback`` fault when ``fetched`` was signed before an index already verified here.

    An old index is still validly signed, so without this a replay could undo a
    blocklist entry. The floor is the verified cache and the newest generation
    ever recorded, which outlives a lost or corrupted cache.
    """
    digest = hashlib.sha256(payload).hexdigest()
    for floor in (_cached_floor(channel, cache, trusted), cache.newest(channel)):
        fault = _older_than(channel, fetched, digest, floor)
        if fault is not None:
            return fault
    return None


def _cached_floor(channel: str, cache: CatalogCache, trusted: Mapping[str, str] | None) -> tuple[datetime, str] | None:
    """The verified cache's signing time and payload sha256; an unreadable cache is no evidence."""
    try:
        cached = cache.load(channel)
        if cached is None:
            return None
        previous = parse_index(cached[0], cached[1], channel, trusted=trusted)
    except CatalogError:
        return None
    return previous.generated, hashlib.sha256(cached[0]).hexdigest()


def _older_than(channel: str, fetched: Catalog, digest: str, floor: tuple[datetime, str] | None) -> CatalogError | None:
    if floor is None or fetched.generated > floor[0]:
        return None
    if fetched.generated == floor[0]:
        if digest == floor[1]:
            return None  # the same generation served again
        # Two different indexes signed at one instant: the order is unknowable,
        # so keep the one already verified rather than let a replay win.
        return CatalogError(
            "rollback",
            f"the {channel} index served now has the same signing time ({fetched.generated_at}) as the "
            "one already verified here but different content; keeping the cached one",
        )
    return CatalogError(
        "rollback",
        f"the {channel} index served now was signed at {fetched.generated_at}, before the one "
        f"already verified here ({floor[0].isoformat()}); keeping the newer one",
    )


def _refresh(
    channel: str,
    url: str,
    cache: CatalogCache,
    fetch: Fetcher,
    trusted: Mapping[str, str] | None,
    now: Callable[[], float],
) -> CatalogRead | CatalogError:
    """Fetch and verify the index; cache and return it, or return why not."""
    try:
        payload = fetch(url)
        signature = fetch(url + ".sig", max_bytes=MAX_SIGNATURE_BYTES)  # what a receipt may hold
        catalog = parse_index(payload, signature, channel, trusted=trusted)
    except CatalogError as exc:
        return exc
    # Compare and store as one step: two overlapping refreshes must not both
    # pass the check against the same cache and then the older one write last.
    with _channel_lock(cache, channel):
        try:
            rollback = _replayed(channel, cache, catalog, payload, trusted)
        except CatalogError as exc:  # a linked replay-floor record
            return exc
        if rollback is not None:
            return rollback
        stamp = now()
        try:
            # The floor first: a cache served without it would be lost evidence if that cache went.
            cache.raise_newest(channel, catalog.generated_at, payload)
            cache.store(channel, payload, signature, stamp)
        except CatalogError as exc:
            return exc
    return CatalogRead(channel, url, catalog, None, stamp, stale=False, payload=payload, signature=signature)


def _behind_record(channel: str, cache: CatalogCache, catalog: Catalog, payload: bytes) -> CatalogError | None:
    """A ``stale-cache`` fault when the cache is older than the newest generation recorded here.

    A newer index verified here whose cache write then failed: the old cache
    must not stand in for it, or a version that index revoked loads again.
    """
    try:
        newest = cache.newest(channel)
    except CatalogError as exc:  # a linked record: fail closed
        return exc
    if newest is None or catalog.generated > newest[0]:
        return None
    if (catalog.generated, hashlib.sha256(payload).hexdigest()) == newest:
        return None
    return CatalogError(
        "stale-cache",
        f"the cached {channel} index (signed {catalog.generated_at}) is older than the newest one verified "
        f"here ({newest[0].isoformat()}), which was never cached; refresh the store",
    )


def read_channel(
    channel: str,
    cache: CatalogCache,
    *,
    fetch: Fetcher = http_fetch,
    base_url: str | None = None,
    refresh: bool = True,
    trusted: Mapping[str, str] | None = None,
    now: Callable[[], float] = time.time,
) -> CatalogRead:
    """Fetch (unless ``refresh`` is False), verify, cache; fall back to the cache."""
    url = f"{base_url or catalog_base_url()}/{channel}.json"
    if not (TRUSTED_STORE_KEYS if trusted is None else trusted):
        # Nothing could be verified, so do not touch the network or the cache.
        return CatalogRead(channel, url, None, CatalogError("no-trusted-key", NO_TRUSTED_KEY_MESSAGE))
    fault: CatalogError | None = None
    if refresh:
        fresh = _refresh(channel, url, cache, fetch, trusted, now)
        if isinstance(fresh, CatalogRead):
            return fresh
        fault = fresh
    try:
        cached = cache.load(channel)
    except CatalogError as exc:  # the cache's own fault: reconnecting cannot repair a damaged cache
        return CatalogRead(channel, url, None, exc)
    if cached is None:
        return CatalogRead(
            channel,
            url,
            None,
            fault or CatalogError("not-fetched", f"no {channel} catalog has been fetched yet"),
        )
    payload, signature, stamp = cached
    try:
        catalog = parse_index(payload, signature, channel, trusted=trusted)
    except CatalogError as exc:
        return CatalogRead(channel, url, None, exc)
    stale = _behind_record(channel, cache, catalog, payload)
    if stale is not None:
        return CatalogRead(channel, url, None, stale)
    return CatalogRead(
        channel, url, catalog, fault, stamp, stale=fault is not None, payload=payload, signature=signature
    )


def read_screenshot(
    shot: Screenshot,
    media_dir: Path,
    *,
    fetch: Fetcher = http_fetch,
    base_url: str | None = None,
) -> bytes:
    """The screenshot's bytes, verified against the signed listing (PLUG-6522k).

    Cached by sha256 under ``media_dir``; a cached file is re-hashed on every
    read, so a file changed on disk is refetched rather than served.
    """
    cached = media_dir / f"{shot.sha256}.{shot.path.rsplit('.', 1)[1]}"
    # A linked media folder, catalog folder or cached file would read and write outside the plugins tree.
    for probe in (media_dir.parent.parent, media_dir.parent, media_dir, cached):
        if probe.is_symlink():
            raise CatalogError("unsafe-cache", f"{probe} is a symlink; refusing to cache screenshots through it")
    held = _read_cached(cached)
    if held is not None and hashlib.sha256(held).hexdigest() == shot.sha256:
        return held
    url = f"{base_url or catalog_base_url()}/{shot.path}"
    data = fetch(url, max_bytes=MAX_SCREENSHOT_BYTES)
    if len(data) != shot.size or hashlib.sha256(data).hexdigest() != shot.sha256:
        raise CatalogError("media-mismatch", f"{url} does not match its signed sha256 and size")
    if sniff_media_type(data) != shot.media_type:
        raise CatalogError("media-mismatch", f"{url} is not the {shot.media_type} its listing names")
    # A unique temp name per writer: the store page asks for the same image
    # from several cards at once, and a shared name lost the race (replace of
    # a temp another request had already moved).
    try:
        media_dir.mkdir(parents=True, exist_ok=True)
        write_atomic(cached, data)
    except OSError as exc:
        raise CatalogError("cache-write-failed", f"could not cache the screenshot at {cached}: {exc}") from exc
    return data


def _read_cached(cached: Path) -> bytes | None:
    """The cached screenshot's bytes, None when none is cached; any other filesystem error is a named fault.

    Reads at most one byte past the screenshot cap: a larger file cannot be a
    signed screenshot, so it reads as not cached and is refetched over.
    """
    try:
        with open_regular_file(cached) as handle:
            data = handle.read(MAX_SCREENSHOT_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CatalogError("cache-read-failed", f"could not read the cached screenshot at {cached}: {exc}") from exc
    return data if len(data) <= MAX_SCREENSHOT_BYTES else None
