"""VUSB-04/05 virtual USB, the borrower's cache: resumable, verified, capped.

Layout, under ``<data_dir>/state/peer-usb-cache/<lender_id>/<lender_stable_id>/``:

- ``audio``            the verified file, served with Range by the borrower's routes
- ``audio.part``       a download in progress; resumed with ``Range: bytes=<size>-``
- ``audio.meta.json``  media type, byte size, sha256, whether a lender hash verified it
- ``track.json``       the lender's export row, so a cached track loads with the peer gone
- ``anlz-<points>.json`` the lender's own ``/anlz`` payload for that ``points``

Nothing here touches ``state.db``: borrowed tracks are never library tracks (VUSB-04).
The cap counts every byte under the cache root (audio, partials, analysis payloads, track
rows and each lender's saved export). Eviction removes whole track entries, least recently
used first (an entry's newest file mtime; serving audio touches it), until the total is
under ``VUSB_CACHE_MAX_BYTES``. It runs after every download attempt, completed or failed
(a failed one keeps its ``.part`` for resume), once more after the entry lock is released
(so concurrent downloads that skipped each other's locked entries still end under the cap),
and after every analysis, track-row or export write (``write_json_capped``). The entry
just written is never evicted; when the cap still cannot be met without it, that file is
removed again and the write refused (``VUSB_CACHE_FULL``). Downloads are admitted against
the cap too: each reserves the bytes it may still write (its per-file cap less its kept
``.part``), so concurrent transfers together never outgrow the cache, and one that does not
fit after eviction is refused (``VUSB_CACHE_FULL``) instead of filling the disk. A lender's saved export (at most 32 MB, one per lender) goes
only once that lender has no cached track left (oldest first): while it has some, it is
what lets them load with the peer gone.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from apps.shared.hashing import sha256_audio_payload, sha256_file

from .peer_usb import PeerUsbError, atomic_write_text, normalize_hash, require_stable_id

#: Size cap for borrowed audio, in bytes (4 GiB: roughly 400 MP3s or 60 WAVs).
VUSB_CACHE_MAX_BYTES = 4 * 1024**3
CACHE_RELPATH = Path("state") / "peer-usb-cache"
#: Hard ceiling on one borrowed file when the lender names no size (a 2 h WAV is ~1.3 GB).
VUSB_FILE_MAX_BYTES = 2 * 1024**3
DOWNLOAD_TIMEOUT_S = httpx.Timeout(10.0, read=30.0)
#: Whole-transfer deadline: a lender dripping bytes cannot hold a worker forever.
DOWNLOAD_DEADLINE_S = 300.0
#: Headroom reserved with every download for the ``audio.meta.json`` written after it
#: (about 150 bytes), so the completed entry still fits the reservation it was admitted on.
META_RESERVE_BYTES = 512
#: How long a second request for the same track waits on a download in flight.
LOCK_WAIT_S = 5.0
AUDIO_NAME = "audio"

_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
#: Downloads in flight: their entry dir -> the per-file cap the audio may grow to. Held
#: until the audio and its sidecar are written and the cap enforced.
_RESERVED: dict[Path, int] = {}
_RESERVE_GUARD = threading.Lock()
#: One eviction or clear at a time: two passes picking the same victim would rmtree it twice.
_EVICT_LOCK = threading.RLock()
#: Entries a response is serving right now -> how many; eviction skips them, clear waits.
_LEASES: Counter[Path] = Counter()


@dataclass(frozen=True)
class CachedAudio:
    path: Path
    media_type: str
    size: int
    verified: bool


def cache_root(data_dir: Path) -> Path:
    return data_dir / CACHE_RELPATH


def require_real_root(root: Path) -> None:
    """A symlinked cache root is refused: containment checks on resolved paths would pass
    for whatever it points at, and eviction deletes ``*/*`` dirs under it."""
    if root.is_symlink():
        raise PeerUsbError("VUSB_CACHE_ROOT_SYMLINK", f"{root} is a symlink: the peer cache must be a real directory")


def _require_real_path_under(root: Path, path: Path) -> None:
    """``path`` lies lexically under ``root`` with no symlinked dir between them (a symlinked
    lender or entry dir would carry the write outside the cache)."""
    # Entry paths arrive resolved (``entry_dir``), the root as configured: match either form.
    base = root if path.is_relative_to(root) else root.resolve()
    if not path.is_relative_to(base):
        raise PeerUsbError("VUSB_CACHE_ROOT_SYMLINK", f"{path} is not under the peer cache {root}")
    step = base
    for part in path.relative_to(base).parts[:-1]:
        step = step / part
        if step.is_symlink():
            raise PeerUsbError("VUSB_CACHE_ROOT_SYMLINK", f"{step} is a symlink inside the peer cache: refusing to write through it")


def entry_dir(root: Path, lender_id: str, stable_id: str) -> Path:
    require_real_root(root)
    path = (root / lender_id / require_stable_id(stable_id)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise PeerUsbError("VUSB_TRACK_ID_INVALID", f"{stable_id!r} escapes the cache")
    return path


@contextmanager
def _entry_lock(entry: Path) -> Iterator[None]:
    """One download per track: a prefetch and a deck load never fetch the same file twice.
    A second caller waits ``LOCK_WAIT_S`` then gets ``VUSB_DOWNLOAD_IN_PROGRESS`` (503),
    so a slow transfer never pins the engine's worker threads."""
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(entry, threading.Lock())
    if not lock.acquire(timeout=LOCK_WAIT_S):
        raise PeerUsbError("VUSB_DOWNLOAD_IN_PROGRESS", f"{entry.name} is still downloading from the peer")
    try:
        yield
    finally:
        lock.release()


# ---------------------------------------------------------------- json sidecars


def read_json(path: Path) -> dict[str, Any] | None:
    """The cached JSON document, or None when absent (including evicted mid-read: a miss)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise PeerUsbError("VUSB_CACHE_CORRUPT", f"{path} is not a JSON object")
    return doc


def write_json(path: Path, doc: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(doc, separators=(",", ":"), sort_keys=True))


def write_json_capped(root: Path, path: Path, doc: dict[str, Any], max_bytes: int = VUSB_CACHE_MAX_BYTES) -> None:
    """``write_json`` for a file under ``root``, then hold the cache to its cap (never
    evicting the entry just written). When the cap cannot be met without that entry, the
    file just written is removed again and ``VUSB_CACHE_FULL`` raised: no entry outgrows it."""
    require_real_root(root)  # before the write: never through a symlinked root
    _require_real_path_under(root, path)
    # Under the admission guard, counting what downloads in flight have reserved: a write
    # must never take bytes a transfer was admitted on, nor interleave with a clear.
    # And under the eviction lock: no pass can remove this entry mid-write.
    with _RESERVE_GUARD, _EVICT_LOCK:
        write_json(path, doc)
        reserved = _reserved_remaining(root, exclude=None)
        evict(root, max_bytes - reserved, keep=path.parent)
        total = _total_bytes(root)
        if total + reserved > max_bytes:
            path.unlink(missing_ok=True)
            raise PeerUsbError(
                "VUSB_CACHE_FULL",
                f"{path.name} does not fit the {max_bytes} byte peer cache ({total} bytes after eviction, {reserved} reserved by downloads)",
            )


# ---------------------------------------------------------------- audio


def cached_audio(entry: Path) -> CachedAudio | None:
    """The entry's verified audio, or None when there is none. A pair that does not hold
    together (sidecar of the wrong shape, a non-audio type, a size that is not the file's)
    is not cached audio: the next download replaces it."""
    audio, meta = entry / AUDIO_NAME, read_json(entry / f"{AUDIO_NAME}.meta.json")
    if meta is None or not _meta_matches(audio, meta):
        return None
    return CachedAudio(audio, meta["media_type"], meta["size"], meta["verified"])


def _meta_matches(audio: Path, meta: dict[str, Any]) -> bool:
    media_type, size, verified = meta.get("media_type"), meta.get("size"), meta.get("verified")
    if not (isinstance(media_type, str) and media_type.startswith("audio/") and isinstance(verified, bool)):
        return False
    # A symlinked ``audio`` is never cached audio: it would serve whatever it points at.
    return isinstance(size, int) and not isinstance(size, bool) and not audio.is_symlink() and _size_or_zero(audio) == size and audio.is_file()


def lease(entry: Path) -> CachedAudio | None:
    """Pin an entry's cached audio for one response: eviction skips it and ``clear`` waits
    until ``release``. None when no audio is cached (never cached, or evicted just now)."""
    with _EVICT_LOCK:
        audio = cached_audio(entry)
        if audio is not None:
            _LEASES[entry.resolve()] += 1
        return audio


def release(entry: Path) -> None:
    """End one ``lease``."""
    key = entry.resolve()
    with _EVICT_LOCK:
        if _LEASES[key] <= 0:
            raise RuntimeError(f"release of {key} without a lease")
        _LEASES[key] -= 1
        if _LEASES[key] == 0:
            del _LEASES[key]


def mark_served(audio: CachedAudio) -> None:
    """Least-recently-served order for eviction is the audio file's mtime."""
    os.utime(audio.path)


#: The served audio type -> the file suffix the tag-free payload hash keys its format on
#: (the download itself is named ``audio.part``, which names no format).
SUFFIX_FOR_MEDIA_TYPE: dict[str, str] = {
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aiff": ".aiff",
    "audio/x-aiff": ".aiff", "audio/flac": ".flac", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
}


def _verify(path: Path, whole_hex: str, expected: list[str], media_type: str) -> bool:
    """True when the bytes match any hash the lender named (whole file or tag-free
    payload, read in the format ``media_type`` names); False when the lender named none.
    A mismatch raises."""
    wanted = {normalize_hash(h) for h in expected}
    if not wanted:
        return False
    if whole_hex in wanted:
        return True
    if normalize_hash(sha256_audio_payload(path, suffix=SUFFIX_FOR_MEDIA_TYPE.get(media_type))) in wanted:
        return True
    raise PeerUsbError("VUSB_HASH_MISMATCH", f"downloaded bytes match none of the lender's hashes {sorted(wanted)}")


def served_media_type(header: str) -> str:
    """The bare media type when it is ``audio/*``, else opaque bytes. Downloads refuse a
    non-audio answer outright (``VUSB_NOT_AUDIO``); this is the second fence for any file
    served from this engine's origin (a text/html would otherwise run as a page here)."""
    media_type = header.split(";", maxsplit=1)[0].strip().lower()
    return media_type if media_type.startswith("audio/") else "application/octet-stream"


def _check_progress(written: int, max_bytes: int, deadline: float) -> None:
    if written > max_bytes:
        raise PeerUsbError("VUSB_FILE_TOO_LARGE", f"the peer sent more than the {max_bytes} byte cap")
    if time.monotonic() > deadline:
        raise PeerUsbError("VUSB_PEER_UNREACHABLE", f"the download outlived {DOWNLOAD_DEADLINE_S:.0f} s")


def _copy_capped(response: httpx.Response, part: Path, mode: str, written: int, max_bytes: int, deadline: float) -> None:
    # No chunk_size: each network read is yielded as it lands, so a peer dripping a few
    # bytes under the read timeout still meets the whole-transfer deadline on every read.
    with part.open(mode) as out:
        for chunk in response.iter_bytes():
            written += len(chunk)
            _check_progress(written, max_bytes, deadline)
            out.write(chunk)


def _write_mode(response: httpx.Response, offset: int) -> str:
    """``ab`` for a resume the peer honored, ``wb`` for a whole file, else the typed refusal."""
    if response.status_code == 206 and offset and response.headers.get("content-range", "").startswith(f"bytes {offset}-"):
        return "ab"
    if response.status_code == 200:
        return "wb"
    if response.status_code in (401, 403):
        raise PeerUsbError("peer_refused", f"the peer refused the audio ({response.status_code})")
    if response.status_code == 404:
        # The body is never read: a hostile 404 could be unbounded or drip forever.
        raise PeerUsbError("VUSB_NOT_SHARED", "the peer does not lend this track (404)")
    raise PeerUsbError("VUSB_PEER_ERROR", f"the peer answered {response.status_code} for audio")


def _download(url: str, headers: dict[str, str], part: Path, max_bytes: int) -> str:
    """Fill ``part`` from ``url``, resuming from its current size, never past ``max_bytes``
    nor ``DOWNLOAD_DEADLINE_S``. Returns the media type to serve. A resume the lender
    answers 416 (the kept ``.part`` is at or past its current file: a crash after the last
    byte, or a file that shrank since) can never succeed, so the ``.part`` is dropped and
    the whole file is fetched once more, instead of refusing that track forever."""
    media_type = _download_once(url, headers, part, max_bytes)
    if media_type is None:
        media_type = _download_once(url, headers, part, max_bytes)
    if media_type is None:
        raise RuntimeError(f"a whole-file request for {url} was answered as a refused resume")
    return media_type


def _download_once(url: str, headers: dict[str, str], part: Path, max_bytes: int) -> str | None:
    """One request; None when a resume was refused with 416 and ``part`` was dropped."""
    offset = part.stat().st_size if part.exists() else 0
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_S
    request_headers = {**headers, **({"Range": f"bytes={offset}-"} if offset else {})}
    with httpx.stream("GET", url, headers=request_headers, timeout=DOWNLOAD_TIMEOUT_S) as response:
        if response.status_code == 416 and offset:
            part.unlink(missing_ok=True)
            return None
        mode = _write_mode(response, offset)
        media_type = served_media_type(response.headers.get("content-type", ""))
        if not media_type.startswith("audio/"):
            raise PeerUsbError("VUSB_NOT_AUDIO", f"the peer answered {response.headers.get('content-type')!r} for audio")
        written = offset if mode == "ab" else 0
        announced = response.headers.get("content-length")
        if announced is not None and announced.isdigit() and written + int(announced) > max_bytes:
            raise PeerUsbError("VUSB_FILE_TOO_LARGE", f"the peer offers {written + int(announced)} bytes, over the {max_bytes} cap")
        try:
            _copy_capped(response, part, mode, written, max_bytes, deadline)
        except PeerUsbError as exc:
            if exc.code == "VUSB_FILE_TOO_LARGE":
                part.unlink(missing_ok=True)
            raise
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise PeerUsbError("VUSB_CACHE_WRITE_FAILED", f"writing {part} failed: {exc}") from exc
    return media_type


def ensure_audio(
    root: Path,
    lender_id: str,
    stable_id: str,
    *,
    peer: Callable[[], tuple[str, dict[str, str]]],
    hashes: list[str],
    size_bytes: int | None,
    max_bytes: int = VUSB_CACHE_MAX_BYTES,
) -> CachedAudio:
    """The cached audio, downloading it first when absent. ``peer`` (url, headers) is only
    called on a miss, so a cached track never needs the peer or its config. Peer failures raise ``VUSB_PEER_UNREACHABLE``."""
    entry = entry_dir(root, lender_id, stable_id)
    hit = cached_audio(entry)
    if hit is not None:
        return hit  # a deck load of a cached track does no cache walk
    try:
        return _ensure_audio_locked(root, entry, peer=peer, hashes=hashes, size_bytes=size_bytes, max_bytes=max_bytes)
    finally:
        # Unlocked now, whether the download worked or not (a failed one keeps its .part):
        # entries that concurrent downloads skipped while locked are evictable.
        evict(root, max_bytes, keep=entry)


def _ensure_audio_locked(
    root: Path,
    entry: Path,
    *,
    peer: Callable[[], tuple[str, dict[str, str]]],
    hashes: list[str],
    size_bytes: int | None,
    max_bytes: int,
) -> CachedAudio:
    with _entry_lock(entry):
        hit = cached_audio(entry)
        if hit is not None:
            return hit
        part = entry / f"{AUDIO_NAME}.part"
        # No valid pair here (checked above): a leftover audio or sidecar from a crash is
        # dropped, so it neither lingers outside the reservation nor reads as cached.
        (entry / AUDIO_NAME).unlink(missing_ok=True)
        (entry / f"{AUDIO_NAME}.meta.json").unlink(missing_ok=True)
        if not hashes:
            # Nothing can verify a resume: a lender file changed between attempts would be
            # spliced (old prefix, new tail) and cached. Without hashes, start over.
            part.unlink(missing_ok=True)
        # A file bigger than the whole cache (less its sidecar's headroom) can never be kept
        # under the cap: it is refused as too large.
        file_cap = min(size_bytes if size_bytes is not None else VUSB_FILE_MAX_BYTES, VUSB_FILE_MAX_BYTES, max_bytes - META_RESERVE_BYTES)
        # The reservation is held until the audio AND its sidecar are written and the cap
        # enforced, so no download admitted meanwhile can take the sidecar's headroom.
        with _admitted(root, entry, file_cap, max_bytes):
            try:
                url, headers = peer()
                media_type = _download(url, headers, part, file_cap)
            except httpx.HTTPError as exc:
                raise PeerUsbError("VUSB_PEER_UNREACHABLE", f"the peer went away mid-download: {type(exc).__name__}: {exc}") from exc
            digest = normalize_hash(sha256_file(part))
            try:
                verified = _verify(part, digest, hashes, media_type)
            except PeerUsbError:
                part.unlink(missing_ok=True)
                raise
            size = part.stat().st_size
            _finalize(entry, part, {"media_type": media_type, "size": size, "sha256": digest, "verified": verified})
            audio = cached_audio(entry)
            if audio is None:
                raise RuntimeError(f"{entry} lost its audio right after writing it")
            evict(root, max_bytes, keep=entry)
            return audio


def _finalize(entry: Path, part: Path, meta: dict[str, Any]) -> None:
    """Promote a verified partial to the cached audio and write its sidecar. A disk that
    fills here is ``VUSB_CACHE_WRITE_FAILED`` (507) with nothing half-cached left behind."""
    try:
        os.replace(part, entry / AUDIO_NAME)
        write_json(entry / f"{AUDIO_NAME}.meta.json", meta)
    except OSError as exc:
        for leftover in (part, entry / AUDIO_NAME, entry / f"{AUDIO_NAME}.meta.json"):
            if leftover.is_file() or leftover.is_symlink():
                leftover.unlink()
        raise PeerUsbError("VUSB_CACHE_WRITE_FAILED", f"finishing {entry.name} in the peer cache failed: {exc}") from exc


@contextmanager
def _admitted(root: Path, entry: Path, file_cap: int, max_bytes: int) -> Iterator[None]:
    """Reserve the bytes this download may still write before it starts, so concurrent
    downloads together stay under ``max_bytes``. Room is made by eviction first; when the
    cache still cannot hold every reservation, ``VUSB_CACHE_FULL`` (nothing is fetched)."""
    with _RESERVE_GUARD:
        others = _reserved_remaining(root, exclude=entry)
        need = max(file_cap + META_RESERVE_BYTES - _download_bytes(entry), 0)
        evict(root, max_bytes - others - need, keep=entry)
        total = _total_bytes(root)
        if total + others + need > max_bytes:
            raise PeerUsbError(
                "VUSB_CACHE_FULL",
                f"the peer cache cannot hold {need} more bytes: {total} cached, {others} reserved by downloads in flight, cap {max_bytes}",
            )
        _RESERVED[entry] = file_cap
        # Created under the guard: ``clear`` holds it too, so it never removes this dir
        # between admission and the first write.
        entry.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        with _RESERVE_GUARD:
            _RESERVED.pop(entry, None)


def _reserved_remaining(root: Path, *, exclude: Path | None) -> int:
    """Bytes downloads in flight UNDER ``root`` may still write (call with ``_RESERVE_GUARD``
    held). Scoped per root: the booth cache and the borrowed-USB cache are separate caches
    with separate caps, so one's transfers never count against the other's room."""
    return sum(
        max(cap + META_RESERVE_BYTES - _download_bytes(other), 0)
        for other, cap in _RESERVED.items()
        if other != exclude and _under(root, other)
    )


def _under(root: Path, entry: Path) -> bool:
    """``entry`` (resolved, as ``entry_dir`` makes it) lies inside the cache ``root``."""
    return entry.is_relative_to(root.resolve())


def _download_bytes(entry: Path) -> int:
    """Bytes a download has already put in its entry: the partial, or the audio and sidecar."""
    return sum(_size_or_zero(entry / name) for name in (f"{AUDIO_NAME}.part", AUDIO_NAME, f"{AUDIO_NAME}.meta.json"))


def _size_or_zero(path: Path) -> int:
    """A file's size, 0 when it is absent or vanished mid-walk (an atomic write's temp file
    renamed, an entry evicted): the walk is a measurement, not a claim it still exists."""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


# ---------------------------------------------------------------- parallel ranged download (B2BBOOTH-40..43)

#: Per-range progress of a parallel download, beside its ``audio.part`` (resume state).
RANGES_NAME = f"{AUDIO_NAME}.ranges.json"
#: How often the range sidecar is rewritten while bytes arrive (a crash loses at most this much).
RANGES_SAVE_S = 1.0
#: Per-request timeouts of one range stream; a stalled stream is reopened from its last byte.
RANGE_TIMEOUT_S = httpx.Timeout(5.0, read=5.0)
class RangeWindow(Protocol):
    """Bounds bytes requested-but-not-received across range streams (the booth's pacer)."""

    def chunk_bytes(self, streams: int) -> int: ...
    def acquire(self, nbytes: int, cancel: threading.Event | None = None) -> None: ...
    def release(self, nbytes: int) -> None: ...


@dataclass(frozen=True)
class FetchControl:
    """How a parallel ranged download runs: its streams, deadline, window, progress and cancel.
    With no window each range is one request for all its bytes; with one, each request asks
    for ``window.chunk_bytes(streams)`` bytes and only once the window grants them."""

    streams: int
    deadline_s: float = DOWNLOAD_DEADLINE_S
    window: RangeWindow | None = None
    on_bytes: Callable[[int], None] | None = None
    cancel: threading.Event | None = None


def plan_ranges(size: int, streams: int) -> list[tuple[int, int]]:
    """``streams`` contiguous inclusive byte ranges covering ``[0, size)`` exactly (fewer when
    the file has fewer bytes than streams). The S3R spike's split, now product code."""
    if size <= 0 or streams <= 0:
        raise ValueError(f"size and streams must be positive, got {size}, {streams}")
    count = min(streams, size)
    edges = [size * i // count for i in range(count + 1)]
    return [(edges[i], edges[i + 1] - 1) for i in range(count)]


@dataclass
class _Range:
    start: int
    end: int  # inclusive
    done: int = 0

    @property
    def left(self) -> int:
        return self.end - self.start + 1 - self.done


def _load_ranges(part: Path, size: int, streams: int) -> tuple[list[_Range], str | None]:
    """``(ranges, media_type)``: the kept progress when ``part`` and its sidecar describe this
    very file and split; otherwise a fresh plan over a fresh ``part`` of ``size`` bytes (a
    stale or foreign sidecar is never trusted: resuming onto it would splice two files)."""
    sidecar = part.with_name(RANGES_NAME)
    doc = read_json(sidecar) if part.exists() else None
    plan = plan_ranges(size, streams)
    if doc is not None and doc.get("size") == size and part.stat().st_size == size:
        kept, media_type = doc.get("ranges"), doc.get("media_type")
        shaped = isinstance(kept, list) and all(isinstance(r, list) and len(r) == 3 for r in kept)
        if shaped and [r[:2] for r in kept] == [list(p) for p in plan] and (media_type is None or isinstance(media_type, str)):
            done = [r[2] for r in kept]
            if all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= e - s + 1 for d, (s, e) in zip(done, plan, strict=True)):
                return [_Range(s, e, d) for (s, e), d in zip(plan, done, strict=True)], media_type
    sidecar.unlink(missing_ok=True)
    with part.open("wb") as out:
        out.truncate(size)
    return [_Range(s, e) for s, e in plan], None


def _save_ranges(part: Path, size: int, ranges: list[_Range], media_type: str | None) -> None:
    write_json(part.with_name(RANGES_NAME), {
        "size": size, "media_type": media_type, "ranges": [[r.start, r.end, r.done] for r in ranges],
    })


def _range_status(response: httpx.Response, offset: int, end: int, size: int) -> None:
    """A range answer must be exactly the bytes asked for, else the typed refusal."""
    if response.status_code in (401, 403):
        raise PeerUsbError("peer_refused", f"the peer refused the audio ({response.status_code})")
    if response.status_code == 404:
        raise PeerUsbError("VUSB_NOT_SHARED", "the peer does not lend this track (404)")
    if response.status_code != 206:
        raise PeerUsbError("VUSB_PEER_ERROR", f"the peer answered {response.status_code} to a byte range")
    if response.headers.get("content-range", "") != f"bytes {offset}-{end}/{size}":
        raise PeerUsbError(
            "VUSB_PEER_ERROR",
            f"the peer answered range {response.headers.get('content-range')!r}, asked bytes {offset}-{end}/{size}",
        )


@dataclass
class _Transfer:
    """Shared state of one parallel download: progress, the first hard failure, a stop flag."""

    part: Path
    size: int
    ranges: list[_Range]
    deadline: float
    control: FetchControl
    on_bytes: Callable[[int], None] | None
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)
    failure: PeerUsbError | None = None
    media_type: str | None = None
    reopens: int = 0

    def fail(self, exc: PeerUsbError) -> None:
        with self.lock:
            if self.failure is None:
                self.failure = exc
        self.stop.set()

    def over(self, cancel: threading.Event | None) -> bool:
        return self.stop.is_set() or time.monotonic() > self.deadline or (cancel is not None and cancel.is_set())


def _stream_range(url: str, headers: dict[str, str], fd: int, rng: _Range, job: _Transfer, cancel: threading.Event | None) -> None:
    """Fill one range at its offset; a broken or stalled stream reopens from its last byte
    until the range is complete, the deadline passes, or another range failed hard."""
    with httpx.Client(timeout=RANGE_TIMEOUT_S) as client:
        while rng.left > 0 and not job.over(cancel):
            try:
                _read_range(client, url, headers, fd, rng, job, cancel)
            except httpx.HTTPError:
                with job.lock:
                    job.reopens += 1
            except PeerUsbError as exc:
                job.fail(exc)
                return
            except OSError as exc:
                job.fail(PeerUsbError("VUSB_CACHE_WRITE_FAILED", f"writing {job.part} failed: {exc}"))
                return


def _read_range(
    client: httpx.Client, url: str, headers: dict[str, str], fd: int, rng: _Range, job: _Transfer, cancel: threading.Event | None
) -> None:
    """One request for the rest of ``rng`` (no window) or for one window grant of it, its
    bytes written at their offsets; every granted byte is released whether it arrived or not."""
    window = job.control.window
    offset = rng.start + rng.done
    want = rng.left if window is None else min(rng.left, window.chunk_bytes(job.control.streams))
    end = offset + want - 1
    if window is not None:
        window.acquire(want, cancel)
    got = 0
    try:
        with client.stream("GET", url, headers={**headers, "Range": f"bytes={offset}-{end}"}) as response:
            _range_status(response, offset, end, job.size)
            media_type = served_media_type(response.headers.get("content-type", ""))
            if not media_type.startswith("audio/"):
                raise PeerUsbError("VUSB_NOT_AUDIO", f"the peer answered {response.headers.get('content-type')!r} for audio")
            with job.lock:
                if job.media_type is None:
                    job.media_type = media_type
            for chunk in response.iter_bytes():
                if got + len(chunk) > want:
                    raise PeerUsbError("VUSB_FILE_TOO_LARGE", f"the peer sent past byte {end} of range {offset}-{end}")
                os.pwrite(fd, chunk, rng.start + rng.done)
                rng.done += len(chunk)
                got += len(chunk)
                if window is not None:
                    window.release(len(chunk))
                if job.on_bytes is not None:
                    job.on_bytes(len(chunk))
                if job.over(cancel):
                    return
    finally:
        if window is not None and got < want:
            window.release(want - got)


def download_ranges(url: str, headers: dict[str, str], part: Path, *, size: int, control: FetchControl) -> tuple[str, int]:
    """Fill ``part`` (``size`` bytes, known up front) from ``url`` as ``control.streams`` parallel Range
    requests written at their offsets, resuming each range from its kept progress
    (``audio.ranges.json``). Returns ``(media_type, reopened_streams)``. A deadline or a
    cancel keeps the ``.part`` and its progress for the next attempt and raises
    ``VUSB_PEER_UNREACHABLE``; a hard refusal from any range stops every range and raises."""
    if size > VUSB_FILE_MAX_BYTES:
        raise PeerUsbError("VUSB_FILE_TOO_LARGE", f"the peer offers {size} bytes, over the {VUSB_FILE_MAX_BYTES} cap")
    on_bytes, cancel, deadline_s = control.on_bytes, control.cancel, control.deadline_s
    ranges, kept_media_type = _load_ranges(part, size, control.streams)
    if on_bytes is not None:
        on_bytes(sum(r.done for r in ranges))
    job = _Transfer(part, size, ranges, time.monotonic() + deadline_s, control, on_bytes, media_type=kept_media_type)
    fd = os.open(part, os.O_WRONLY)
    try:
        workers = [
            threading.Thread(target=_stream_range, args=(url, headers, fd, r, job, cancel), daemon=True, name=f"booth-range-{i}")
            for i, r in enumerate(ranges)
            if r.left > 0
        ]
        for worker in workers:
            worker.start()
        while any(w.is_alive() for w in workers):
            for worker in workers:
                worker.join(timeout=RANGES_SAVE_S / len(workers))
            _save_ranges(part, size, ranges, job.media_type)
    finally:
        os.close(fd)
    _save_ranges(part, size, ranges, job.media_type)
    if job.failure is not None:
        if job.failure.code == "VUSB_FILE_TOO_LARGE":
            part.unlink(missing_ok=True)
            part.with_name(RANGES_NAME).unlink(missing_ok=True)
        raise job.failure
    missing = sum(r.left for r in ranges)
    if missing:
        why = "was cancelled" if cancel is not None and cancel.is_set() else f"outlived {deadline_s:.0f} s"
        raise PeerUsbError("VUSB_PEER_UNREACHABLE", f"the download {why} with {missing} of {size} bytes missing (kept for resume)")
    if job.media_type is None:
        raise RuntimeError(f"{part} completed with no media type recorded")
    return job.media_type, job.reopens


def _verify_or_drop(part: Path, sha256: str, media_type: str) -> str:
    """The whole-file sha256 of ``part`` when it is ``sha256`` (or its tag-free payload is),
    else the partial and its progress are dropped (never resumed onto) and
    ``VUSB_HASH_MISMATCH`` raised."""
    digest = normalize_hash(sha256_file(part))
    try:
        verified = _verify(part, digest, [sha256], media_type)
    except PeerUsbError:
        part.unlink(missing_ok=True)
        part.with_name(RANGES_NAME).unlink(missing_ok=True)
        raise
    if not verified:
        raise RuntimeError("a booth transfer always names a hash")
    return digest


def ensure_ranged_audio(
    root: Path,
    lender_id: str,
    stable_id: str,
    *,
    peer: Callable[[], tuple[str, dict[str, str]]],
    sha256: str,
    size_bytes: int,
    max_bytes: int,
    control: FetchControl,
) -> CachedAudio:
    """``ensure_audio`` for a file whose size and sha256 are known up front (a committed booth
    ``load``): fetched as ``streams`` parallel ranges, whole-file sha256 verified BEFORE it is
    cached (a mismatch drops the ``.part`` and raises ``VUSB_HASH_MISMATCH``). Same entry
    lock, admission and eviction as ``ensure_audio``, against ``root``'s own ``max_bytes``."""
    entry = entry_dir(root, lender_id, stable_id)
    hit = cached_audio(entry)
    if hit is not None and hit.verified:
        return hit
    try:
        with _entry_lock(entry):
            hit = cached_audio(entry)
            if hit is not None and hit.verified:
                return hit
            (entry / AUDIO_NAME).unlink(missing_ok=True)
            (entry / f"{AUDIO_NAME}.meta.json").unlink(missing_ok=True)
            part = entry / f"{AUDIO_NAME}.part"
            with _admitted(root, entry, size_bytes, max_bytes):
                url, headers = peer()
                media_type, _reopens = download_ranges(url, headers, part, size=size_bytes, control=control)
                digest = _verify_or_drop(part, sha256, media_type)
                part.with_name(RANGES_NAME).unlink(missing_ok=True)
                _finalize(entry, part, {"media_type": media_type, "size": size_bytes, "sha256": digest, "verified": True})
                audio = cached_audio(entry)
                if audio is None:
                    raise RuntimeError(f"{entry} lost its audio right after writing it")
                return audio
    finally:
        evict(root, max_bytes, keep=entry)


# ---------------------------------------------------------------- eviction and status


@dataclass(frozen=True)
class _Entry:
    path: Path
    size: int
    last_used: float
    has_audio: bool
    has_part: bool


def _entries(root: Path) -> list[_Entry]:
    """Every track entry (``<lender_id>/<stable_id>/``) with the bytes of all its files."""
    if not root.exists():
        return []
    out: list[_Entry] = []
    real_root = root.resolve()
    for found in root.glob("*/*"):
        # Never followed: a symlinked lender or entry dir is not cache, so it is never
        # measured or deleted (eviction would otherwise rmtree the link's target).
        if found.is_symlink() or found.parent.is_symlink():
            continue
        entry = real_root / found.parent.name / found.name
        try:
            stats = {p.name: p.stat() for p in entry.iterdir() if p.is_file()} if entry.is_dir() else {}
        except FileNotFoundError:
            continue  # evicted or renamed while this walk ran
        if stats:
            out.append(_Entry(
                entry, sum(st.st_size for st in stats.values()), _last_served(stats),
                AUDIO_NAME in stats, f"{AUDIO_NAME}.part" in stats,
            ))
    return out


def _last_served(stats: dict[str, os.stat_result]) -> float:
    """Least recently SERVED orders eviction: the audio mtime (``mark_served`` touches it),
    so a row or anlz write never makes an unplayed track look recent. No audio: newest file."""
    return stats[AUDIO_NAME].st_mtime if AUDIO_NAME in stats else max(st.st_mtime for st in stats.values())


def _total_bytes(root: Path) -> int:
    """Every byte under the cache root, entries and per-lender files alike."""
    return sum(_size_or_zero(p) for p in root.rglob("*") if p.is_file()) if root.exists() else 0


def _download_in_flight(entry: Path) -> bool:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(entry)
    return lock is not None and lock.locked()


def evict(root: Path, max_bytes: int, *, keep: Path | None = None) -> list[Path]:
    """Delete least recently used track entries, whole, until every byte under ``root`` is
    within ``max_bytes``. ``keep`` (an entry dir) and entries mid-download are skipped."""
    require_real_root(root)
    with _EVICT_LOCK:
        return _evict_locked(root, max_bytes, keep=None if keep is None else keep.resolve())


def _evict_locked(root: Path, max_bytes: int, *, keep: Path | None) -> list[Path]:
    total = _total_bytes(root)
    evicted: list[Path] = []
    for entry in sorted(_entries(root), key=lambda e: e.last_used):
        if total <= max_bytes:
            break
        if entry.path == keep or entry.path in _LEASES or _download_in_flight(entry.path):
            continue
        if entry.path.exists():
            shutil.rmtree(entry.path)
            total -= entry.size
            evicted.append(entry.path)
    if total > max_bytes and root.exists():
        evicted += _evict_orphan_lenders(root, total - max_bytes, keep=keep)
    return evicted


def _orphan_lenders(root: Path) -> list[Path]:
    """Lender dirs holding no track entry; a symlink is never one (it is not cache)."""
    real_root = root.resolve()
    return [real_root / d.name for d in root.iterdir() if not d.is_symlink() and d.is_dir() and not _has_cached_entry(d)]


def _has_cached_entry(lender: Path) -> bool:
    """Any entry dir holding a file (an empty one, left by a refused download, is not)."""
    return any(p.is_dir() and any(p.iterdir()) for p in lender.iterdir())


def _evict_orphan_lenders(root: Path, over: int, *, keep: Path | None) -> list[Path]:
    """Last resort: lender dirs holding no track entry (just a saved export), oldest first.
    A lender with cached tracks keeps its export: it is what loads them with the peer gone."""
    evicted: list[Path] = []
    for lender in sorted(_orphan_lenders(root), key=lambda d: max((p.stat().st_mtime for p in d.iterdir()), default=0.0)):
        if over <= 0:
            break
        if keep is not None and keep.is_relative_to(lender):
            continue
        over -= sum(p.stat().st_size for p in lender.iterdir() if p.is_file())
        shutil.rmtree(lender)
        evicted.append(lender)
    return evicted


def status(root: Path, max_bytes: int = VUSB_CACHE_MAX_BYTES) -> dict[str, Any]:
    require_real_root(root)
    entries = _entries(root)
    return {
        "root": str(root),
        "entries": sum(1 for e in entries if e.has_audio),
        "bytes": _total_bytes(root),
        "max_bytes": max_bytes,
        "partial_downloads": sum(1 for e in entries if e.has_part),
    }


def clear(root: Path) -> int:
    """Delete the whole cache, or ``VUSB_DOWNLOAD_IN_PROGRESS`` while any transfer is in
    flight (removing its ``.part`` under it would fail that load, or the clear on Windows).
    Holds the admission guard, so no new download starts while it deletes."""
    require_real_root(root)
    with _RESERVE_GUARD:
        with _LOCKS_GUARD:
            busy = [entry.name for entry, lock in _LOCKS.items() if lock.locked() and _under(root, entry)]
        reserved = [entry for entry in _RESERVED if _under(root, entry)]
        if reserved or busy:
            in_flight = len(reserved) or len(busy)
            raise PeerUsbError("VUSB_DOWNLOAD_IN_PROGRESS", f"{in_flight} peer download(s) in flight: clear the cache once they finish")
        with _EVICT_LOCK:
            leased = [entry for entry in _LEASES if _under(root, entry)]
            if leased:
                raise PeerUsbError("VUSB_CACHE_IN_USE", f"{len(leased)} cached track(s) are being served: clear the cache once they finish")
            count = sum(1 for e in _entries(root) if e.has_audio)
            if root.exists():
                shutil.rmtree(root)
            return count
