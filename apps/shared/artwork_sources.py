"""Artwork for tracks rekordbox has none for: a folder image, then an online cover.

The ``/tracks/{id}/artwork`` chain is rekordbox's own jpg, then the picture
embedded in the audio file (:func:`apps.shared.audio_files.read_embedded_artwork`),
then the two sources here:

1. **Folder image.** ``cover``/``folder``/``front``/``album``/``artwork`` with a
   jpg, png or webp extension, beside the audio file. Read only.
2. **Online cover.** MusicBrainz finds the recording by artist + title (+
   duration), and the Cover Art Archive serves the release group's front
   cover. Both are free with no key (MusicBrainz core data is CC0); MusicBrainz
   asks for at most one request per second and a descriptive User-Agent.
   Measured on bifrost's library Thu 1 Oct 2026 (research/artwork/,
   scripts/artworkbench): 21% of tracks have a cover there.

What a lookup found, or that it found nothing, is cached under the app's own
data directory (``<state dir>/artwork-cache/``). Nothing is ever written into
the library or into an audio file: those folders are synced (Syncthing) and
rekordbox-owned. A miss is remembered for :data:`MISS_TTL_S` so a track with
no online cover costs one lookup a week, not one per view.

``ODJ_ARTWORK_ONLINE=0`` turns the online lookup off (no request leaves the
machine); the cache is still served.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from . import audio_files

USER_AGENT = "OpenDJ/1.0 ( https://github.com/private_owner/music-dj-tools )"
MB_URL = "https://musicbrainz.org/ws/2/recording"
CAA_URL = "https://coverartarchive.org/release-group/{rg}/front-250"
MB_SPACING_S = 1.1
HTTP_TIMEOUT_S = 6.0
DURATION_TOLERANCE_S = 5.0
MAX_RELEASE_GROUPS = 3
MISS_TTL_S = 7 * 24 * 3600
# After a network failure every lookup is skipped for this long, so an offline
# machine never holds a request thread on a dead connection per deck view.
OFFLINE_BACKOFF_S = 120.0
# A deck asks for two sizes at once; the second waits this long for the first
# lookup of the same track (its worst case), then answers without a cover.
INFLIGHT_WAIT_S = HTTP_TIMEOUT_S * (MAX_RELEASE_GROUPS + 2)

SIDECAR_STEMS = ("cover", "folder", "front", "album", "artwork")
SIDECAR_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

_FEAT = re.compile(r"\s*[\(\[]?\s*(feat\.?|ft\.?|featuring)\s.*$", re.I)
_MIX = re.compile(r"\s*[\(\[]\s*(original|extended|club|radio)\s*(mix|edit|version)\s*[\)\]]", re.I)
# Mixed In Key style prefixes: "6A - 7 - Title", "6A - Title", "Am - 5 - Title".
_KEY_PREFIX = re.compile(
    r"^\s*(?:\d{1,2}[AB]\s*-\s*(?:\d{1,2}\s*-\s*)?|[A-G][#b]?m?\s*-\s*\d{1,2}\s*-\s*)"
)


# ----- folder image -----------------------------------------------------------


def sidecar_artwork(audio_path: Path) -> tuple[bytes, str] | None:
    """A named cover image beside ``audio_path``, as ``(bytes, mime)``, or None.

    Only the conventional names count: a lone unrelated image in a shared
    downloads folder is not this track's cover. The bytes must match the
    extension's own image format and stay under the embedded-art size cap.
    """
    for candidate in _sidecar_candidates(audio_path.parent):
        image = _read_image(candidate, SIDECAR_EXTS[candidate.suffix.lower()])
        if image is not None:
            return image
    return None


def sidecar_artwork_path(audio_path: Path) -> Path | None:
    """The first named cover image beside ``audio_path`` (cheap: no read)."""
    candidates = _sidecar_candidates(audio_path.parent)
    return candidates[0] if candidates else None


def _sidecar_candidates(directory: Path) -> tuple[Path, ...]:
    """Named cover images in ``directory``, best name first.

    Memoized on the directory's mtime: a listing asks once per row, and a
    folder of 10,000 tracks must be scanned once, not 10,000 times. Adding or
    removing a file moves the directory's mtime, which drops the memo.
    """
    try:
        mtime_ns = directory.stat().st_mtime_ns
    except OSError:
        return ()
    return _sidecar_candidates_at(str(directory), mtime_ns)


@functools.lru_cache(maxsize=4096)
def _sidecar_candidates_at(directory: str, _mtime_ns: int) -> tuple[Path, ...]:
    by_stem: dict[str, list[Path]] = {}
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                suffix = os.path.splitext(entry.name)[1].lower()
                if suffix in SIDECAR_EXTS:
                    stem = os.path.splitext(entry.name)[0].lower()
                    if stem in SIDECAR_STEMS:
                        by_stem.setdefault(stem, []).append(Path(entry.path))
    except OSError:
        return ()
    return tuple(p for stem in SIDECAR_STEMS for p in sorted(by_stem.get(stem, ())))


def _read_image(path: Path, mime: str) -> tuple[bytes, str] | None:
    try:
        if not path.is_file() or path.stat().st_size > audio_files.MAX_EMBEDDED_ARTWORK_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    return (data, mime) if audio_files._is_safe_raster_image(data[:12], mime) else None


# ----- online cover -----------------------------------------------------------


@dataclass(frozen=True)
class TrackQuery:
    """What an online lookup knows about a track."""

    artist: str | None
    title: str | None
    duration_ms: int | None


def online_enabled() -> bool:
    return os.environ.get("ODJ_ARTWORK_ONLINE", "1").strip().lower() not in {"0", "false", "no", "off"}


def query_title(title: str | None) -> str:
    """Title as a store would list it: drop a leading key/energy tag and 'Original Mix'."""
    return _MIX.sub("", _KEY_PREFIX.sub("", title or "")).strip()


def query_artist(artist: str | None) -> str:
    """First credited artist of a comma/ampersand/feat. joined list."""
    return re.split(
        r"\s*(?:,|;|&| x | feat\.? | ft\.? )\s*", artist or "", maxsplit=1, flags=re.I
    )[0].strip()


def normalize(text: str | None) -> str:
    """Lower-case, strip accents, feat. lists and 'Original Mix' noise."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = _FEAT.sub("", _MIX.sub("", t))
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def matching_release_groups(payload: dict, query: TrackQuery) -> list[str]:
    """Release groups of recordings that are this track, best first.

    A recording counts only when its normalized title equals ours, its
    artist credit contains our first artist (or the other way round), and
    its length is within :data:`DURATION_TOLERANCE_S` when both are known.
    Without the artist check, common titles match a stranger's recording.

    The artist's own official album, single or EP comes first, then other
    plain releases, and soundtracks, compilations, DJ mixes and live albums
    last, so a hit's cover is its own sleeve rather than a film poster.
    """
    title = normalize(query_title(query.title))
    ours = normalize(query_artist(query.artist))
    want_s = (query.duration_ms or 0) / 1000.0
    best: dict[str, int] = {}
    for rec in payload.get("recordings", []):
        if not _is_same_recording(rec, title, ours, want_s):
            continue
        for rel in rec.get("releases", []):
            group = (rel.get("release-group") or {}).get("id")
            if group:
                best[group] = min(best.get(group, 3), _release_tier(rel, ours))
    # sorted() is stable, so within a tier MusicBrainz's own order holds.
    return sorted(best, key=best.__getitem__)


_OWN_TYPES = {"album", "single", "ep"}


def _release_tier(rel: dict, ours: str) -> int:
    """0: the artist's own official album/single/EP; 1: other plain; 2: secondary types."""
    group = rel.get("release-group") or {}
    if group.get("secondary-types"):
        return 2
    credit = normalize(" ".join(c.get("name", "") for c in rel.get("artist-credit", [])))
    own = (
        rel.get("status") == "Official"
        and str(group.get("primary-type") or "").lower() in _OWN_TYPES
        and bool(ours) and ours in credit
    )
    return 0 if own else 1


def _is_same_recording(rec: dict, title: str, ours: str, want_s: float) -> bool:
    if normalize(rec.get("title")) != title:
        return False
    credit = normalize(" ".join(c.get("name", "") for c in rec.get("artist-credit", [])))
    if not credit or not ours or (ours not in credit and credit not in ours):
        return False
    rec_s = (rec.get("length") or 0) / 1000.0
    return not (want_s and rec_s and abs(rec_s - want_s) > DURATION_TOLERANCE_S)


class ArtworkCache:
    """Found covers and remembered misses, one pair of files per track."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _base(self, stable_id: str) -> Path:
        return self.root / hashlib.sha256(stable_id.encode()).hexdigest()[:32]

    def get(self, stable_id: str) -> tuple[bytes, str] | None:
        meta = self._meta(stable_id)
        if meta is None or meta.get("status") != "found":
            return None
        return _read_image(self._base(stable_id).with_suffix(".img"), str(meta.get("mime")))

    def has(self, stable_id: str) -> bool:
        meta = self._meta(stable_id)
        return meta is not None and meta.get("status") == "found"

    def recent_miss(self, stable_id: str, now: float) -> bool:
        meta = self._meta(stable_id)
        return (
            meta is not None
            and meta.get("status") == "miss"
            and now - float(meta.get("checked_epoch", 0)) < MISS_TTL_S
        )

    def put_found(self, stable_id: str, data: bytes, mime: str, release_group: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        base = self._base(stable_id)
        _write_atomic(base.with_suffix(".img"), data)
        self._put_meta(stable_id, {"status": "found", "mime": mime, "source": "coverartarchive",
                                   "release_group": release_group})

    def put_miss(self, stable_id: str, reason: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._put_meta(stable_id, {"status": "miss", "reason": reason})

    def _put_meta(self, stable_id: str, meta: dict) -> None:
        meta = {**meta, "stable_id": stable_id, "checked_epoch": time.time(),
                "checked_utc": datetime.now(UTC).isoformat(timespec="seconds")}
        _write_atomic(self._base(stable_id).with_suffix(".json"), json.dumps(meta).encode())

    def _meta(self, stable_id: str) -> dict | None:
        try:
            return json.loads(self._base(stable_id).with_suffix(".json").read_bytes())
        except (OSError, ValueError):
            return None


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


NETWORK_FAILURE = -1
BUSY = -2  # our own MusicBrainz queue was full: try again on a later view


class HttpGet(Protocol):
    """What the lookup needs from HTTP: status and body, or a negative status of our own."""

    def get(self, url: str) -> tuple[int, bytes]: ...


class _Http:
    """GET with MusicBrainz's one-request-per-second spacing, shared process-wide."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_mb = 0.0

    def get(self, url: str) -> tuple[int, bytes]:
        host = urllib.parse.urlsplit(url).hostname or ""
        if host == "musicbrainz.org":
            # Bounded wait: a queue of lookups must not pile up request threads.
            if not self._lock.acquire(timeout=HTTP_TIMEOUT_S):
                return BUSY, b""
            try:
                wait = self._last_mb + MB_SPACING_S - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                return self._fetch(url)
            finally:
                self._last_mb = time.monotonic()
                self._lock.release()
        return self._fetch(url)

    @staticmethod
    def _fetch(url: str) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return resp.status, resp.read(audio_files.MAX_EMBEDDED_ARTWORK_BYTES + 1)
        except urllib.error.HTTPError as exc:
            return exc.code, b""
        except (urllib.error.URLError, TimeoutError, OSError):
            return NETWORK_FAILURE, b""


_HTTP = _Http()
_INFLIGHT: dict[str, threading.Lock] = {}
_INFLIGHT_GUARD = threading.Lock()


class _Backoff:
    until = 0.0  # time.monotonic() before which lookups are skipped


_BACKOFF = _Backoff()


def online_cover(cache: ArtworkCache, stable_id: str, query: TrackQuery,
                 http: HttpGet | None = None) -> tuple[bytes, str] | None:
    """The cached online cover, looking it up first when it was never tried.

    Returns None when the lookup is off, the track has no artist or title,
    a recent lookup already missed, or nothing matched. A network failure is
    NOT remembered as a miss: lookups pause for :data:`OFFLINE_BACKOFF_S`
    and a later view tries again. A view that arrives while the same track
    is already being looked up waits for that lookup (bounded by
    :data:`INFLIGHT_WAIT_S`) and serves its result rather than a second one.
    """
    hit = cache.get(stable_id)
    if hit is not None:
        return hit
    if not online_enabled() or not query_title(query.title) or not query_artist(query.artist):
        return None
    if time.monotonic() < _BACKOFF.until:
        return None
    with _INFLIGHT_GUARD:
        lock = _INFLIGHT.setdefault(stable_id, threading.Lock())
    if not lock.acquire(timeout=INFLIGHT_WAIT_S):
        return None  # nothing learned, so nothing cached
    try:
        hit = cache.get(stable_id)
        if hit is not None or cache.recent_miss(stable_id, time.time()):
            return hit
        if time.monotonic() < _BACKOFF.until:
            return None  # the lookup we waited on found the network down
        result, offline = _lookup(cache, stable_id, query, http or _HTTP)
        if offline:
            _BACKOFF.until = time.monotonic() + OFFLINE_BACKOFF_S
        return result
    finally:
        lock.release()


def _lookup(cache: ArtworkCache, stable_id: str, query: TrackQuery,
            http: HttpGet) -> tuple[tuple[bytes, str] | None, bool]:
    """(cover or None, whether the network was unreachable)."""
    lucene = f'recording:"{_lucene(query_title(query.title))}" AND artist:"{_lucene(query_artist(query.artist))}"'
    status, body = http.get(f"{MB_URL}?fmt=json&limit=100&query={urllib.parse.quote(lucene)}")
    if status != 200:
        # unreachable, busy or rate-limited: not a fact about the track
        return None, status == NETWORK_FAILURE
    try:
        groups = matching_release_groups(json.loads(body), query)
    except ValueError:
        return None, False
    if not groups:
        cache.put_miss(stable_id, "no matching recording")
        return None, False
    for group in groups[:MAX_RELEASE_GROUPS]:
        status, data = http.get(CAA_URL.format(rg=group))
        if status == 200 and len(data) <= audio_files.MAX_EMBEDDED_ARTWORK_BYTES:
            mime = _sniff_mime(data)
            if mime is not None:
                cache.put_found(stable_id, data, mime, group)
                return (data, mime), False
        elif status not in (200, 404):
            return None, status == NETWORK_FAILURE  # transient: retry on a later view
    cache.put_miss(stable_id, "no front cover on the matching release groups")
    return None, False


def _lucene(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _sniff_mime(data: bytes) -> str | None:
    for mime in ("image/jpeg", "image/png", "image/webp"):
        if audio_files._is_safe_raster_image(data[:12], mime):
            return mime
    return None
