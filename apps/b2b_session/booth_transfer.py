"""B2BBOOTH-40..49 shared booth track transfer, engine side.

Contract: docs/b2b-link/BOOTH-CONTRACT.md v3 "Resolving a load". Every laptop renders every
deck, so on a committed ``load`` EACH laptop's engine resolves the track:

- the loading DJ's own laptop: its own library id (``owner_track_id``), and it LENDS that id to
  the partner for this epoch (``lends``; the export routes honor it beside lent playlists);
- the other laptop: its own library first, by audio sha256 (``find_owned``; a hit is never
  transferred), else the file from the loading DJ's laptop over the virtual USB audio route
  through the paired door, as ``streams_for(size)`` parallel Range streams (16, or 32 above
  32 MB: docs/b2b-link/SPIKE3-RERUN.md), whole-file sha256 verified BEFORE it is ready, into
  the BOOTH cache (``<data_dir>/state/booth-cache``, separate from the borrowed-USB cache).
  The resolved id is the virtual USB borrower id ``usb-peer-<lender_id>-<owner_track_id>``,
  so the deck-load path (``/api/v1/b2b/usb/tracks/{id}``) works unchanged.

The booth cache is deleted whole on ``end``, an epoch change, a booth close or an unpair
(``close``): booth-received files never outlive the set (B2BBOOTH-46).

Pacing (B2BBOOTH-44): a transfer fills the link queue and inflates the round trip that control
traffic pays (S3R: 10 -> 35-61 ms at 5 ms one-way). ``Pacer`` is a receiver-driven scavenger:
LEDBAT's delay target (RFC 6817) applied the rLEDBAT way (RFC 9840: the RECEIVER bounds what the
sender may send), at application level. Range streams request window-sized sub-ranges only
while bytes requested-but-not-received stay under one shared window, so the queue the sender
can build is bounded by it. A door round-trip probe measures the queueing delay over the lowest
round trip seen (per-minute minima, 10 minutes) through LEDBAT's noise filter (the minimum of
the last two samples); the window shrinks when that delay exceeds ``CFG.TARGET_QDELAY_MS`` and
grows while it is below and the streams actually fill it (window validation, RFC 7661). Throttling READS was measured first and does
not bound the queue: the kernel fills each socket's receive buffer regardless
(docs/b2b-link/BOOTH-TRANSFER.md, rounds 1-3).

Hook into the engine booth (S1b; it cannot import this package: import-linter keeps the B2B
siblings independent, so the web UI layer wires it, ``b2b_booth_transfer_hook``): the booth's
commit watcher wakes a thread that runs ``BoothTransfers.sync_from(booth)`` (committed ops into
``observe``), and ``BoothTransfers.close(reason)`` runs when the booth closes or the pairing is
forgotten. ``deck_resolution(deck)`` is what the booth state shows per deck.
"""

from __future__ import annotations

import logging
import math
import queue
import shutil
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Protocol

import httpx

from apps.shared.hashing import HashCache

from . import peer_usb
from . import peer_usb_cache as cache
from .peer_usb import PeerUsbError

log = logging.getLogger(__name__)


class CFG:
    #: Range streams per track, and for files over ``LARGE_FILE_BYTES`` (S3R: 94.2% -> 100%
    #: verified within 10 s at 1% loss / 25 ms one-way; every N=16 miss was a 42-60 MB file).
    STREAMS: int = 16
    STREAMS_LARGE: int = 32
    LARGE_FILE_BYTES: int = 32 * 1024**2
    CACHE_RELPATH: Path = Path("state") / "booth-cache"
    #: Booth cache cap: a long set of lossless tracks, all deleted after the set anyway.
    CACHE_MAX_BYTES: int = 8 * 1024**3
    #: Whole-transfer deadline for one track (a resume picks up the kept ranges).
    DEADLINE_S: float = 300.0
    #: The lender applies the same commit a moment after us; its 404 is retried this long
    #: before the track reads "not found" (its lend is not in place yet).
    GRANT_RACE_S: float = 3.0
    GRANT_RETRY_S: float = 0.1
    #: How long ``close`` waits for an in-flight transfer to stop before deleting under it.
    STOP_WAIT_S: float = 10.0
    # ---- pacing (B2BBOOTH-44): chosen from the rig data in docs/b2b-link/BOOTH-TRANSFER.md
    PACING_ENABLED: bool = True
    #: Token-free door route answered by the door itself (no engine hop): a clean round trip.
    PROBE_PATH: str = "/b2b-pair/hello"
    PROBE_ACTIVE_HZ: float = 10.0
    PROBE_IDLE_HZ: float = 1.0
    PROBE_TIMEOUT_S: float = 2.0
    #: Queueing delay the transfer may add to the round trip (the control bar is one RTT +
    #: 50 ms). Rig rounds 7-8 (door round trip instrument): 30 ms met the 10 s gate in every run
    #: at 1% / 25 ms; 10 ms and a 768 KiB window cap broke it. Rounds 10-11 (the booth control
    #: lane through the engine, one way, 0.1% / 5 ms, bar 64 ms): p95 89 ms at 30, 75 at 20, 64 at
    #: 15, while the 10 s share at 1% / 25 ms fell 99.2% -> 98.3% -> 95.0%. 15 is the only value
    #: that met both bars on the rig, both by a thin margin (docs/b2b-link/BOOTH-TRANSFER.md).
    TARGET_QDELAY_MS: float = 15.0
    #: Bytes requested and not yet received, across every range stream of every transfer
    #: (an application-level receive window, the rLEDBAT idea, RFC 9840): the sender can
    #: never put more than this on the link, so the queue it can build is bounded by it.
    WINDOW_START_BYTES: int = 512 * 1024
    WINDOW_MIN_BYTES: int = 64 * 1024
    WINDOW_MAX_BYTES: int = 64 * 1024**2
    #: One range request asks for window / streams bytes, within these bounds. Each request
    #: costs one round trip of idle stream, so a chunk is never tiny (rig round 2: 32 KiB
    #: chunks halved goodput at 25 ms one-way), and never so big that a window of a few MB
    #: keeps most of 32 streams idle (round 5: 128 KiB).
    CHUNK_MIN_BYTES: int = 64 * 1024
    CHUNK_MAX_BYTES: int = 1024**2
    #: Per probe sample: window *= 1 - gain * over (over the target) or 1 + gain * under.
    #: (A slow-start gain of 0.5 until the first cut was measured in rounds 6-7: no faster
    #: to ready, a worse control-lane tail; removed.)
    DECREASE_GAIN: float = 0.3
    INCREASE_GAIN: float = 0.15
    #: Base round trip = minimum over this many one-minute buckets (LEDBAT's base history).
    BASE_HISTORY_MIN: int = 10
    #: The delay the controller acts on is the minimum of the last few samples (LEDBAT's noise
    #: filter): one probe that waited out a TCP retransmit is loss, not a queue (rig round 4:
    #: cutting on single spikes at 1% loss held the window at its start); a queue persists.
    DELAY_FILTER: int = 2
    #: Congestion window validation (RFC 7661): the window grows only when the streams had at
    #: least this share of it outstanding since the last sample, so a loss-limited transfer
    #: that cannot fill it never inflates it into a flood for the first loss-free moment.
    VALIDATION_SHARE: float = 0.9
    #: Delivered bytes/s is reported over this window (status only).
    DELIVERY_WINDOW_S: float = 1.0
    #: A blocked grant re-checks its cancel this often.
    WAIT_SLICE_S: float = 0.25


def streams_for(size: int) -> int:
    """Range streams for a file of ``size`` bytes: 16, or 32 above 32 MB (S3R)."""
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")
    return CFG.STREAMS_LARGE if size > CFG.LARGE_FILE_BYTES else CFG.STREAMS


def booth_root(data_dir: Path) -> Path:
    return data_dir / CFG.CACHE_RELPATH


def booth_entry(data_dir: Path, lender_id: str, stable_id: str) -> Path | None:
    """The booth cache entry for a borrower id's parts when the booth received (or is
    receiving) that track, else None (the borrowed-USB cache serves it)."""
    root = booth_root(data_dir)
    entry = cache.entry_dir(root, lender_id, stable_id)
    return entry if (entry / "track.json").is_file() else None


# ---------------------------------------------------------------- own library by sha256


def library_owners(state_db: Path, sha256: str, size: int, hash_cache: Path) -> list[str]:
    """Stable ids in the library at ``state_db`` whose audio is the file with ``sha256``.

    First the stored ``tracks.content_hash`` (whole-file sha256, with or without the
    ``sha256:`` prefix) and ``audio_hash`` where the column exists. Only on a miss, rows with
    no stored hash are checked lazily: a ``stat`` of each, and a sha256 (cached by path, size
    and mtime in ``hash_cache``) only of files whose size equals ``size``. Never a library
    rehash: a load hashes at most the few files of exactly the same byte count."""
    if not state_db.exists():
        return []
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
        wanted = (f"sha256:{sha256}", sha256)
        hits: list[str] = []
        for col in (c for c in ("content_hash", "audio_hash") if c in columns):
            hits += [
                sid
                for (sid,) in conn.execute(
                    f"SELECT stable_id FROM tracks WHERE {col} IN (?, ?) AND deleted_at IS NULL ORDER BY stable_id",
                    wanted,
                )
                if sid not in hits
            ]
        if hits:
            return hits
        unhashed = conn.execute(
            "SELECT stable_id, file_path FROM tracks WHERE content_hash IS NULL AND deleted_at IS NULL "
            "AND file_path IS NOT NULL ORDER BY stable_id"
        ).fetchall()
    finally:
        conn.close()
    same_size = [(sid, Path(fp)) for sid, fp in unhashed if _size_or_none(Path(fp)) == size]
    if not same_size:
        return []
    with HashCache(hash_cache) as hashes:
        return [sid for sid, path in same_size if peer_usb.normalize_hash(hashes.hash_with_cache(path)) == sha256]


def _size_or_none(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


# ---------------------------------------------------------------- pacing


class Pacer:
    """The delay-based window shared by every range stream of every booth transfer.

    Streams ``acquire(n)`` before requesting ``n`` bytes and ``release`` them as they arrive,
    so bytes requested-but-not-received never exceed ``window``. ``on_rtt`` feeds door round
    trips (ms) and moves the window. Disabled, it still records round trips (the instrument),
    and ``BoothTransfers`` does not gate on it at all (one request per range)."""

    def __init__(self, *, enabled: bool, clock: Callable[[], float] = time.monotonic) -> None:
        self.enabled = enabled
        self._clock = clock
        self._cond = threading.Condition()
        self.window = CFG.WINDOW_START_BYTES
        self.outstanding = 0
        self._peak = 0
        self._minutes: dict[int, float] = {}
        self._recent: deque[float] = deque(maxlen=CFG.DELAY_FILTER)
        self._arrivals: deque[tuple[float, int]] = deque()
        self._active = 0
        self.samples = 0
        self.cuts = 0
        self.last_rtt_ms: float | None = None
        self.qdelay_ms: float | None = None

    # -- round trips and the controller

    def base_rtt_ms(self) -> float | None:
        with self._cond:
            return min(self._minutes.values()) if self._minutes else None

    def on_rtt(self, rtt_ms: float) -> None:
        if not (math.isfinite(rtt_ms) and rtt_ms >= 0):
            raise ValueError(f"a round trip must be a finite non-negative ms value, got {rtt_ms!r}")
        now = self._clock()
        with self._cond:
            minute = int(now // 60)
            self._minutes[minute] = min(self._minutes.get(minute, rtt_ms), rtt_ms)
            for old in [m for m in self._minutes if m <= minute - CFG.BASE_HISTORY_MIN]:
                del self._minutes[old]
            self._recent.append(rtt_ms)
            self.samples += 1
            self.last_rtt_ms = rtt_ms
            base = min(self._minutes.values())
            self.qdelay_ms = min(self._recent) - base
            used, self._peak = self._peak, self.outstanding
            if not (self.enabled and self._active and len(self._recent) == CFG.DELAY_FILTER):
                return
            if self.qdelay_ms > CFG.TARGET_QDELAY_MS:
                over = min(1.0, (self.qdelay_ms - CFG.TARGET_QDELAY_MS) / CFG.TARGET_QDELAY_MS)
                self.cuts += 1
                self.window = max(CFG.WINDOW_MIN_BYTES, round(self.window * (1 - CFG.DECREASE_GAIN * over)))
                return
            if used >= CFG.VALIDATION_SHARE * self.window and self.qdelay_ms < CFG.TARGET_QDELAY_MS:
                under = (CFG.TARGET_QDELAY_MS - self.qdelay_ms) / CFG.TARGET_QDELAY_MS
                self.window = round(min(CFG.WINDOW_MAX_BYTES, self.window * (1 + CFG.INCREASE_GAIN * under)))
                self._cond.notify_all()

    def begin(self) -> None:
        with self._cond:
            self._active += 1

    def end(self) -> None:
        with self._cond:
            if self._active <= 0:
                raise RuntimeError("Pacer.end without a begin")
            self._active -= 1

    @property
    def active(self) -> bool:
        return self._active > 0

    def _delivered_bps_locked(self, now: float) -> float:
        while self._arrivals and self._arrivals[0][0] < now - CFG.DELIVERY_WINDOW_S:
            self._arrivals.popleft()
        return sum(n for _t, n in self._arrivals) / CFG.DELIVERY_WINDOW_S

    # -- the window (peer_usb_cache.RangeWindow)

    def chunk_bytes(self, streams: int) -> int:
        """Bytes one range request asks for: the window split across the streams."""
        return max(CFG.CHUNK_MIN_BYTES, min(CFG.CHUNK_MAX_BYTES, self.window // max(1, streams)))

    def acquire(self, nbytes: int, cancel: threading.Event | None = None) -> None:
        """Block until ``nbytes`` more may be requested (always at once when nothing is
        outstanding, so a grant larger than the window never deadlocks)."""
        with self._cond:
            while self.outstanding and self.outstanding + nbytes > self.window:
                if cancel is not None and cancel.is_set():
                    raise PeerUsbError(
                        "VUSB_PEER_UNREACHABLE", "the transfer was cancelled while waiting for the window"
                    )
                self._cond.wait(CFG.WAIT_SLICE_S)
            self.outstanding += nbytes
            self._peak = max(self._peak, self.outstanding)

    def release(self, nbytes: int) -> None:
        """``nbytes`` of a grant arrived (or never will): room for the next request."""
        with self._cond:
            if nbytes > self.outstanding:
                raise RuntimeError(f"release of {nbytes} bytes with only {self.outstanding} outstanding")
            self.outstanding -= nbytes
            self._arrivals.append((self._clock(), nbytes))
            self._cond.notify_all()

    def status(self) -> dict[str, Any]:
        base = self.base_rtt_ms()
        if not self.enabled:
            mode = "off"
        elif base is None:
            mode = "unmeasured (no door round trip yet): held at the start window"
        else:
            mode = "delay-based window"
        with self._cond:
            delivered = self._delivered_bps_locked(self._clock())
        return {
            "enabled": self.enabled,
            "mode": mode,
            "window_bytes": self.window if self.enabled else None,
            "outstanding_bytes": self.outstanding,
            "delivered_bytes_per_s": round(delivered),
            "target_qdelay_ms": CFG.TARGET_QDELAY_MS,
            "base_rtt_ms": None if base is None else round(base, 2),
            "last_rtt_ms": None if self.last_rtt_ms is None else round(self.last_rtt_ms, 2),
            "qdelay_ms": None if self.qdelay_ms is None else round(self.qdelay_ms, 2),
            "samples": self.samples,
            "cuts": self.cuts,
            "active": self.active,
        }


class RttProbe:
    """Door round trips at ``PROBE_ACTIVE_HZ`` while a transfer runs, ``PROBE_IDLE_HZ`` otherwise
    (the idle samples are what make the base round trip a no-transfer one). One keep-alive
    connection, as a control lane holds one."""

    def __init__(self, pacer: Pacer, peer: Callable[[], tuple[str, dict[str, str]]]) -> None:
        self._pacer = pacer
        self._peer = peer
        self._stop = threading.Event()
        self.errors = 0
        self.last_error: str | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="booth-rtt-probe")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=CFG.PROBE_TIMEOUT_S + 1)

    def _run(self) -> None:
        with httpx.Client(timeout=CFG.PROBE_TIMEOUT_S) as client:
            while not self._stop.is_set():
                started = perf_counter()
                try:
                    base, _headers = self._peer()
                    client.get(f"{base}{CFG.PROBE_PATH}").raise_for_status()
                    self._pacer.on_rtt((perf_counter() - started) * 1000.0)
                except (httpx.HTTPError, PeerUsbError) as exc:
                    self.errors += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                hz = CFG.PROBE_ACTIVE_HZ if self._pacer.active else CFG.PROBE_IDLE_HZ
                self._stop.wait(max(0.0, 1.0 / hz - (perf_counter() - started)))


# ---------------------------------------------------------------- transfers

JobState = Literal["queued", "resolving", "receiving", "ready", "owned", "local", "failed"]


@dataclass
class Job:
    """One track the booth loaded: how this laptop resolves it."""

    owner_track_id: str
    by: str
    sha256: str
    size: int
    title: str
    artist: str
    bpm: float | None
    key: str | None
    duration_s: float
    decks: set[int] = field(default_factory=set)
    state: JobState = "queued"
    received: int = 0
    stable_id: str | None = None
    reason: str | None = None
    error: str | None = None
    streams: int | None = None
    queued_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    ready_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, nbytes: int) -> None:
        with self._lock:
            self.received += nbytes

    @property
    def percent(self) -> int:
        return min(100, int(self.received * 100 / self.size)) if self.size else 0

    def resolution(self) -> dict[str, Any]:
        """What the booth state shows for a deck holding this track (contract v3: the
        resolved ``stable_id``, or the reason: ``resolving``, ``receiving NN%``, ``not found``)."""
        if self.state in ("ready", "owned", "local"):
            return {"state": self.state, "stable_id": self.stable_id, "reason": None}
        if self.state == "receiving":
            return {
                "state": "receiving",
                "stable_id": None,
                "reason": f"receiving {self.percent}%",
                "percent": self.percent,
            }
        if self.state == "failed":
            return {"state": "failed", "stable_id": None, "reason": self.reason}
        return {"state": self.state, "stable_id": None, "reason": "resolving"}

    def as_dict(self) -> dict[str, Any]:
        now = time.monotonic()
        end = self.ready_at if self.ready_at is not None else now
        return {
            "owner_track_id": self.owner_track_id,
            "by": self.by,
            "sha256": self.sha256,
            "bytes": self.size,
            "title": self.title,
            "decks": sorted(self.decks),
            "state": self.state,
            "received": self.received,
            "percent": self.percent,
            "streams": self.streams,
            "stable_id": self.stable_id,
            "reason": self.reason,
            "error": self.error,
            "elapsed_s": None if self.started_at is None else round(end - self.started_at, 3),
            "load_to_ready_s": None if self.ready_at is None else round(self.ready_at - self.queued_at, 3),
        }

    def row(self) -> dict[str, Any]:
        """The ``track.json`` the virtual USB routes read (``peer_usb.validate_track_row``)."""
        return {
            "stable_id": self.owner_track_id,
            "title": self.title,
            "artist": self.artist,
            "album": None,
            "genre": None,
            "key": self.key,
            "bpm": self.bpm,
            "duration_s": self.duration_s,
            "rating": None,
            "content_hash": f"sha256:{self.sha256}",
            "audio_hash": None,
            "size_bytes": self.size,
        }


class BoothLog(Protocol):
    """What ``sync_from`` reads from the engine booth (``apps.b2b_booth.booth.Booth``, duck-typed:
    import-linter keeps ``apps.b2b_session`` from importing ``apps.b2b_booth``)."""

    side: str | None

    def log_since(self, seq: int) -> dict[str, Any]: ...


class BoothTransfers:
    """One per engine. Resolves committed booth loads and holds the booth cache for one epoch.

    ``peer`` gives the paired partner's door base URL and auth headers; ``find_owned(sha256,
    size)`` this laptop's own playable copy (a stable id) or None; ``peer_lender_id`` the
    partner's virtual USB lender id. Transfers run one at a time on one worker thread (the
    S3R basis: one deck load at a time)."""

    def __init__(
        self,
        data_dir: Path,
        *,
        peer: Callable[[], tuple[str, dict[str, str]]],
        find_owned: Callable[[str, int], str | None],
        peer_lender_id: Callable[[], str],
        pacing: bool = CFG.PACING_ENABLED,
        probe: bool = True,
        max_bytes: int = CFG.CACHE_MAX_BYTES,
    ) -> None:
        self.data_dir = data_dir
        self.root = booth_root(data_dir)
        self._peer = peer
        self._find_owned = find_owned
        self._peer_lender_id = peer_lender_id
        self._max_bytes = max_bytes
        self.pacer = Pacer(enabled=pacing)
        self._probe_enabled = probe
        self._probe: RttProbe | None = None
        self._lock = threading.RLock()
        self.epoch: str | None = None
        self.side: str | None = None
        self.seen_seq = 0
        self.lending: set[str] = set()
        self.jobs: dict[str, Job] = {}
        self.decks: dict[int, str] = {}
        self._lender_id: str | None = None
        #: Separate from ``_lock``: the worker reads it while ``close`` holds ``_lock`` and joins it.
        self._lender_lock = threading.Lock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self.wipes = 0
        self.last_wipe: dict[str, Any] | None = None

    # -- the hook

    def sync_from(self, booth: BoothLog) -> None:
        """Catch up with the engine booth's committed log (what the hook's sync thread runs after the
        booth's commit watcher fires): the ops since the last one seen, the whole log again on an
        epoch change, and ``close`` once the booth has no epoch (it closed on this laptop)."""
        head = booth.log_since(0 if self.epoch is None else self.seen_seq)
        if head["epoch"] is None:
            if self.epoch is not None:
                self.close("booth closed")
            return
        if head["epoch"] != self.epoch:
            head = booth.log_since(0)
        self.observe(head["epoch"], booth.side, head["ops"])

    def observe(self, epoch: str | None, side: str | None, committed: list[dict[str, Any]]) -> None:
        """Committed ops of ``epoch`` in seq order, as this laptop (``side``) applied them.
        A new epoch first deletes everything the last one received; ``end`` deletes it too."""
        with self._lock:
            if epoch != self.epoch:
                if self.epoch is not None:
                    self._wipe_locked(f"epoch {self.epoch} -> {epoch}")
                self.epoch, self.side, self.seen_seq = epoch, side, 0
            for item in committed:
                if item["seq"] <= self.seen_seq:
                    continue
                if item["seq"] != self.seen_seq + 1:
                    raise ValueError(
                        f"booth transfer saw seq {item['seq']} after {self.seen_seq}: committed ops arrive in order"
                    )
                self.seen_seq = item["seq"]
                op = item["op"]
                if op["op"] == "load":
                    self._on_load(item["by"], op["deck"], op["track"])
                elif op["op"] == "end":
                    self._wipe_locked("end")

    def close(self, reason: str) -> dict[str, Any]:
        """The booth closed or the pairing was forgotten: delete every booth-received file."""
        with self._lock:
            summary = self._wipe_locked(reason)
            self.epoch, self.side, self.seen_seq = None, None, 0
            return summary

    def lends(self, stable_id: str) -> bool:
        """True when this laptop loaded ``stable_id`` onto a booth deck this epoch, so the
        partner may fetch it (the export routes' scope, beside lent playlists)."""
        with self._lock:
            return stable_id in self.lending

    def deck_resolution(self, deck: int) -> dict[str, Any] | None:
        with self._lock:
            key = self.decks.get(deck)
            return None if key is None else self.jobs[key].resolution()

    # -- loads

    def _on_load(self, by: str, deck: int, track: dict[str, Any]) -> None:
        sid = peer_usb.require_stable_id(track["owner_track_id"])
        key = f"{by}:{sid}"
        job = self.jobs.get(key)
        if job is None:
            job = Job(
                sid,
                by,
                track["sha256"],
                track["bytes"],
                track["title"],
                track["artist"],
                track["bpm"],
                track["key"],
                track["duration_s"],
            )
            self.jobs[key] = job
            if by == self.side:
                # My own track: I play it from my library and lend it to the partner.
                self.lending.add(sid)
                job.state, job.stable_id, job.ready_at = "local", sid, time.monotonic()
            else:
                self._ensure_worker()
                self._queue.put(key)
        for other in self.jobs.values():
            other.decks.discard(deck)
        job.decks.add(deck)
        self.decks[deck] = key

    def _ensure_worker(self) -> None:
        if self._probe_enabled and self._probe is None:
            self._probe = RttProbe(self.pacer, self._peer)
            self._probe.start()
        if self._worker is None or not self._worker.is_alive():
            self._cancel = threading.Event()
            self._worker = threading.Thread(
                target=self._work, args=(self._queue, self._cancel), daemon=True, name="booth-transfer"
            )
            self._worker.start()

    def _work(self, jobs: queue.Queue[str | None], cancel: threading.Event) -> None:
        while not cancel.is_set():
            key = jobs.get()
            if key is None or cancel.is_set():
                return
            job = self.jobs.get(key)  # never under ``_lock``: a wipe holds it while it joins this thread
            if job is None:
                continue
            try:
                self._resolve(job, cancel)
            except PeerUsbError as exc:
                job.state, job.error = "failed", exc.message
                job.reason = "not found" if exc.code == "VUSB_NOT_SHARED" else f"failed: {exc.code}"
                log.warning("booth transfer: %s failed (%s)", job.owner_track_id, exc)
            except Exception as exc:  # recorded on the job and logged with its traceback, never swallowed
                job.state, job.reason, job.error = "failed", f"failed: {type(exc).__name__}", str(exc)[:500]
                log.exception("booth transfer: %s crashed", job.owner_track_id)

    def _resolve(self, job: Job, cancel: threading.Event) -> None:
        job.state, job.started_at = "resolving", time.monotonic()
        owned = self._find_owned(job.sha256, job.size)
        if owned is not None:
            job.state, job.stable_id, job.ready_at = "owned", owned, time.monotonic()
            return
        lender_id = self._lender()
        entry = cache.entry_dir(self.root, lender_id, job.owner_track_id)
        cache.write_json_capped(self.root, entry / "track.json", job.row(), self._max_bytes)
        job.streams = streams_for(job.size)
        job.state = "receiving"
        self.pacer.begin()
        try:
            self._fetch_with_grant_race(job, lender_id, cancel)
        finally:
            self.pacer.end()
        job.received = job.size
        job.state, job.stable_id, job.ready_at = (
            "ready",
            peer_usb.peer_track_id(lender_id, job.owner_track_id),
            time.monotonic(),
        )

    def _fetch_with_grant_race(self, job: Job, lender_id: str, cancel: threading.Event) -> None:
        give_up = time.monotonic() + CFG.GRANT_RACE_S
        control = cache.FetchControl(
            streams=job.streams or streams_for(job.size),
            deadline_s=CFG.DEADLINE_S,
            window=self._window(),
            on_bytes=job.add,
            cancel=cancel,
        )
        while True:
            job.received = 0
            try:
                cache.ensure_ranged_audio(
                    self.root,
                    lender_id,
                    job.owner_track_id,
                    peer=lambda: self._audio_url(job),
                    sha256=job.sha256,
                    size_bytes=job.size,
                    max_bytes=self._max_bytes,
                    control=control,
                )
            except PeerUsbError as exc:
                if exc.code != "VUSB_NOT_SHARED" or time.monotonic() > give_up or cancel.is_set():
                    raise
                time.sleep(CFG.GRANT_RETRY_S)
            else:
                return

    def _window(self) -> Pacer | None:
        """The window the range streams gate on: none when pacing is off (one request per range)."""
        return self.pacer if self.pacer.enabled else None

    def _audio_url(self, job: Job) -> tuple[str, dict[str, str]]:
        base, headers = self._peer()
        return f"{base}/api/v1/b2b/usb/export/tracks/{job.owner_track_id}/audio", headers

    def _lender(self) -> str:
        with self._lender_lock:
            if self._lender_id is None:
                self._lender_id = self._peer_lender_id()
            return self._lender_id

    # -- delete after the set

    def _wipe_locked(self, reason: str) -> dict[str, Any]:
        """Stop the worker (cancel, wait), forget every job and lend, delete the booth cache."""
        self._cancel.set()
        self._queue.put(None)
        worker, self._worker = self._worker, None
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=CFG.STOP_WAIT_S)
            if worker.is_alive():
                raise RuntimeError(
                    f"booth transfer worker did not stop within {CFG.STOP_WAIT_S} s; the booth cache is NOT deleted"
                )
        if self._probe is not None:
            self._probe.stop()
            self._probe = None
        self._queue = queue.Queue()
        files = [p for p in self.root.rglob("*") if p.is_file()] if self.root.exists() else []
        cache.require_real_root(self.root)
        if self.root.exists():
            shutil.rmtree(self.root)
        self.jobs.clear()
        self.decks.clear()
        self.lending.clear()
        with self._lender_lock:
            self._lender_id = None
        self.wipes += 1
        self.last_wipe = {"reason": reason, "files_deleted": len(files), "at_monotonic": round(time.monotonic(), 3)}
        return self.last_wipe

    # -- status

    def status(self) -> dict[str, Any]:
        with self._lock:
            files = sorted(p for p in self.root.rglob("*") if p.is_file()) if self.root.exists() else []
            cached = {
                "root": str(self.root),
                "files": [str(f) for f in files],
                "bytes": sum(_size_or_none(f) or 0 for f in files),
            }
            return {
                "epoch": self.epoch,
                "side": self.side,
                "seen_seq": self.seen_seq,
                "cache": cached | {"max_bytes": self._max_bytes},
                "lending": sorted(self.lending),
                "tracks": [job.as_dict() for job in self.jobs.values()],
                "decks": {str(d): self.jobs[k].resolution() for d, k in sorted(self.decks.items())},
                "pacing": self.pacer.status()
                | {
                    "probe_errors": self._probe.errors if self._probe else 0,
                    "probe_last_error": self._probe.last_error if self._probe else None,
                },
                "wipes": self.wipes,
                "last_wipe": self.last_wipe,
            }
