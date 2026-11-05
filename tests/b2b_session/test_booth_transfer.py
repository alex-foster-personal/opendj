"""B2BBOOTH-40..48: shared booth track transfer, engine side (``apps.b2b_session.booth_transfer``).

Real HTTP throughout: the lender is a ``ThreadingHTTPServer`` on loopback answering Range
requests exactly as the virtual USB export audio route does (206, Content-Range, audio/*);
the receiver is the product ``BoothTransfers`` driven by committed booth ops.

[if] a file is 32 MB or less [then] 16 streams, above it 32, [else stop].
[if] the received bytes do not hash to the load's sha256 [then] the deck never reads ready and nothing is cached, [else stop].
[if] streams break mid-range [then] each reopens from its last byte and the file still verifies, [else stop].
[if] a transfer is interrupted [then] the next attempt fetches only the missing bytes, [else stop].
[if] the booth ends, changes epoch or closes [then] every booth-received file is gone and files outside the booth cache stay, [else stop].
[if] this laptop owns the same sha256 [then] nothing is fetched, [else stop].
[if] queueing delay persists over the target [then] the window shrinks, below it the window grows, [else stop].
"""

from __future__ import annotations

import hashlib
import itertools
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from apps.b2b_session import booth_transfer as bt
from apps.b2b_session import peer_usb_cache as cache
from apps.b2b_session.peer_usb import PeerUsbError

pytestmark = [pytest.mark.requirement(f"B2BBOOTH-{n}") for n in range(40, 49)]

LENDER_ID = "0123456789ab"
EPOCH_A = "a" * 16
EPOCH_B = "b" * 16
SID = "owner-track-1"
_RANGE = re.compile(r"^bytes=(\d+)-(\d+)$")


# ---------------------------------------------------------------- a real lender


@dataclass
class Lender:
    """What the fake lender serves and what it saw. Knobs are read per request."""

    files: dict[str, bytes]
    corrupt: bool = False
    #: Close each response after this many body bytes (a broken stream), when set.
    cut_after: int | None = None
    #: Answer 404 until this monotonic time (the lender has not applied the commit yet).
    not_found_until: float = 0.0
    #: Sleep this long per 64 KiB written (a slow link), when set.
    drip_s: float = 0.0
    requests: list[str] = field(default_factory=list)
    served: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def _handler(lender: Lender) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            with lender.lock:
                lender.requests.append(self.path)
            if self.path == "/b2b-pair/hello":
                self._send(200, b'{"v":1}', "application/json")
                return
            match = re.fullmatch(r"/api/v1/b2b/usb/export/tracks/([A-Za-z0-9_-]+)/audio", self.path)
            body = lender.files.get(match.group(1)) if match else None
            if body is None or time.monotonic() < lender.not_found_until:
                self._send(404, b'{"detail":{"code":"VUSB_NOT_SHARED"}}', "application/json")
                return
            if lender.corrupt:
                body = bytes([body[0] ^ 0xFF]) + body[1:]
            wanted = _RANGE.fullmatch(self.headers.get("Range", ""))
            if wanted is None:
                self._send(200, body, "audio/mpeg")
                return
            start, end = int(wanted.group(1)), int(wanted.group(2))
            chunk = body[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.end_headers()
            limit = len(chunk) if lender.cut_after is None else min(len(chunk), lender.cut_after)
            for i in range(0, limit, 65536):
                piece = chunk[i : min(limit, i + 65536)]
                self.wfile.write(piece)
                with lender.lock:
                    lender.served += len(piece)
                if lender.drip_s:
                    time.sleep(lender.drip_s)
            if limit < len(chunk):
                self.close_connection = True
                self.connection.shutdown(2)

        def _send(self, status: int, body: bytes, media: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@pytest.fixture
def lender() -> Iterator[tuple[Lender, str]]:
    state = Lender(files={})
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _audio(size: int, seed: int = 7) -> bytes:
    return (hashlib.sha256(str(seed).encode()).digest() * (size // 32 + 1))[:size]


def _load(seq: int, by: str, deck: int, body: bytes, sid: str = SID) -> dict[str, Any]:
    return {
        "seq": seq,
        "by": by,
        "pid": None,
        "op": {
            "op": "load",
            "deck": deck,
            "track": {
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "title": "T",
                "artist": "A",
                "bpm": 124.0,
                "key": "8A",
                "duration_s": 200.0,
                "owner_track_id": sid,
            },
        },
    }


def _end(seq: int) -> dict[str, Any]:
    return {"seq": seq, "by": "inviter", "pid": None, "op": {"op": "end"}}


def _transfers(tmp_path: Path, base: str, owned: dict[str, str] | None = None, **kw: Any) -> bt.BoothTransfers:
    return bt.BoothTransfers(
        tmp_path,
        peer=lambda: (base, {"Authorization": "Bearer t"}),
        find_owned=lambda sha, _size: (owned or {}).get(sha),
        peer_lender_id=lambda: LENDER_ID,
        probe=False,
        **kw,
    )


def _wait(transfers: bt.BoothTransfers, deck: int, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = transfers.deck_resolution(deck)
        if res is not None and res["state"] in ("ready", "owned", "local", "failed"):
            return res
        time.sleep(0.02)
    raise AssertionError(f"deck {deck} did not settle: {transfers.status()}")


# ---------------------------------------------------------------- N selection and chunk planning


def test_streams_for_picks_16_up_to_32_mb_and_32_above() -> None:
    assert bt.streams_for(1) == 16
    assert bt.streams_for(32 * 1024**2) == 16
    assert bt.streams_for(32 * 1024**2 + 1) == 32
    assert bt.streams_for(60 * 1024**2) == 32
    with pytest.raises(ValueError, match="positive"):
        bt.streams_for(0)


@pytest.mark.parametrize(("size", "streams"), [(1, 16), (15, 16), (16, 16), (1000, 16), (10_000_019, 32)])
def test_plan_ranges_covers_the_file_exactly_once(size: int, streams: int) -> None:
    ranges = cache.plan_ranges(size, streams)
    assert len(ranges) == min(size, streams)
    assert ranges[0][0] == 0 and ranges[-1][1] == size - 1
    for (_, end), (start, _) in itertools.pairwise(ranges):
        assert start == end + 1
    assert sum(e - s + 1 for s, e in ranges) == size


# ---------------------------------------------------------------- verify before ready


def test_a_partner_track_is_fetched_verified_and_resolves_to_the_borrower_id(
    tmp_path: Path, lender: tuple[Lender, str]
) -> None:
    state, base = lender
    body = _audio(3_000_001)
    state.files[SID] = body
    transfers = _transfers(tmp_path, base, pacing=False)
    transfers.observe(EPOCH_A, "joiner", [_load(1, "inviter", 1, body)])
    res = _wait(transfers, 1)
    assert res == {"state": "ready", "stable_id": f"usb-peer-{LENDER_ID}-{SID}", "reason": None}
    audio = cache.cached_audio(cache.entry_dir(bt.booth_root(tmp_path), LENDER_ID, SID))
    assert audio is not None and audio.verified
    assert audio.path.read_bytes() == body
    ranged = [r for r in state.requests if "/audio" in r]
    assert len(ranged) == 16, "unpaced: one Range request per stream (paced: window sub-ranges, tested below)"
    track = transfers.status()["tracks"][0]
    assert track["streams"] == 16 and track["percent"] == 100


def test_bytes_that_do_not_match_the_sha256_never_read_ready(tmp_path: Path, lender: tuple[Lender, str]) -> None:
    state, base = lender
    body = _audio(500_000)
    state.files[SID] = body
    state.corrupt = True
    transfers = _transfers(tmp_path, base)
    transfers.observe(EPOCH_A, "joiner", [_load(1, "inviter", 2, body)])
    res = _wait(transfers, 2)
    assert res["state"] == "failed" and res["reason"] == "failed: VUSB_HASH_MISMATCH" and res["stable_id"] is None
    entry = cache.entry_dir(bt.booth_root(tmp_path), LENDER_ID, SID)
    assert cache.cached_audio(entry) is None
    assert not (entry / "audio.part").exists(), "a mismatched partial is dropped, never resumed"


def test_receiving_shows_progress_before_ready(tmp_path: Path, lender: tuple[Lender, str]) -> None:
    state, base = lender
    body = _audio(2_000_000)
    state.files[SID] = body
    state.drip_s = 0.02
    transfers = _transfers(tmp_path, base)
    transfers.observe(EPOCH_A, "joiner", [_load(1, "inviter", 3, body)])
    seen: set[str] = set()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        res = transfers.deck_resolution(3)
        assert res is not None
        seen.add(res["reason"] or res["state"])
        if res["state"] == "ready":
            break
        time.sleep(0.01)
    assert any(r.startswith("receiving ") and r.endswith("%") for r in seen), seen
    assert "ready" in seen


# ---------------------------------------------------------------- resume


def test_broken_streams_reopen_from_their_last_byte(tmp_path: Path, lender: tuple[Lender, str]) -> None:
    state, base = lender
    body = _audio(1_600_000)
    state.files[SID] = body
    state.cut_after = 40_000  # each range is 100 KB: every stream breaks twice or more
    transfers = _transfers(tmp_path, base)
    transfers.observe(EPOCH_A, "joiner", [_load(1, "inviter", 1, body)])
    assert _wait(transfers, 1)["state"] == "ready"
    assert state.served == len(body), "no byte is fetched twice across reopens"
    assert len([r for r in state.requests if "/audio" in r]) > 16


def test_an_interrupted_download_resumes_with_only_the_missing_bytes(
    tmp_path: Path, lender: tuple[Lender, str]
) -> None:
    state, base = lender
    body = _audio(2_000_000)
    state.files[SID] = body
    root = bt.booth_root(tmp_path)
    cancel = threading.Event()
    state.drip_s = 0.01

    def cancel_soon() -> None:
        while state.served < 500_000:
            time.sleep(0.005)
        cancel.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    sha = hashlib.sha256(body).hexdigest()
    peer = lambda: (f"{base}/api/v1/b2b/usb/export/tracks/{SID}/audio", {})  # noqa: E731
    with pytest.raises(PeerUsbError) as first:
        cache.ensure_ranged_audio(
            root,
            LENDER_ID,
            SID,
            peer=peer,
            sha256=sha,
            size_bytes=len(body),
            max_bytes=10**9,
            control=cache.FetchControl(streams=16, cancel=cancel),
        )
    assert first.value.code == "VUSB_PEER_UNREACHABLE" and "kept for resume" in first.value.message
    entry = cache.entry_dir(root, LENDER_ID, SID)
    assert (entry / "audio.part").exists() and (entry / cache.RANGES_NAME).exists()
    served_first = state.served
    assert 0 < served_first < len(body)
    state.drip_s = 0.0
    audio = cache.ensure_ranged_audio(
        root,
        LENDER_ID,
        SID,
        peer=peer,
        sha256=sha,
        size_bytes=len(body),
        max_bytes=10**9,
        control=cache.FetchControl(streams=16),
    )
    assert audio.path.read_bytes() == body
    # Only the missing bytes, plus at most one in-flight chunk per stream.
    assert state.served - served_first <= len(body) - served_first + 16 * 65536
    assert not (entry / cache.RANGES_NAME).exists()


def test_a_sidecar_for_another_split_is_not_trusted(tmp_path: Path) -> None:
    part = tmp_path / "audio.part"
    part.write_bytes(b"\0" * 100)
    cache._save_ranges(part, 100, [cache._Range(0, 99, 100)], "audio/mpeg")
    ranges, media = cache._load_ranges(part, 100, 4)
    assert [r.done for r in ranges] == [0, 0, 0, 0] and media is None
    cache._save_ranges(part, 100, [cache._Range(s, e, e - s + 1) for s, e in cache.plan_ranges(100, 4)], "audio/mpeg")
    ranges, media = cache._load_ranges(part, 100, 4)
    assert [r.done for r in ranges] == [25, 25, 25, 25] and media == "audio/mpeg"


# ---------------------------------------------------------------- delete after the set


def _booth_files(tmp_path: Path) -> list[Path]:
    root = bt.booth_root(tmp_path)
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


@pytest.mark.parametrize("how", ["end", "epoch", "close"])
def test_booth_files_are_deleted_after_the_set_and_nothing_else(
    tmp_path: Path, lender: tuple[Lender, str], how: str
) -> None:
    state, base = lender
    body = _audio(400_000)
    state.files[SID] = body
    control = cache.cache_root(tmp_path) / LENDER_ID / "kept-usb-track" / "audio"
    control.parent.mkdir(parents=True)
    control.write_bytes(b"borrowed usb audio")
    library_file = tmp_path / "library" / "own.mp3"
    library_file.parent.mkdir()
    library_file.write_bytes(b"mine")
    transfers = _transfers(tmp_path, base)
    transfers.observe(EPOCH_A, "joiner", [_load(1, "inviter", 1, body)])
    assert _wait(transfers, 1)["state"] == "ready"
    during = _booth_files(tmp_path)
    assert any(p.name == "audio" for p in during) and any(p.name == "track.json" for p in during)
    assert all(p.exists() for p in during)
    if how == "end":
        transfers.observe(EPOCH_A, "joiner", [_end(2)])
    elif how == "epoch":
        transfers.observe(EPOCH_B, "joiner", [])
    else:
        transfers.close("unpair")
    assert [p for p in during if p.exists()] == [], "every path listed during the set is gone"
    assert not bt.booth_root(tmp_path).exists()
    assert control.read_bytes() == b"borrowed usb audio", "the borrowed-USB cache is not the booth cache"
    assert library_file.read_bytes() == b"mine"
    assert transfers.status()["last_wipe"]["files_deleted"] == len(during)
    assert transfers.deck_resolution(1) is None


def test_a_transfer_in_flight_is_stopped_and_its_partial_deleted(tmp_path: Path, lender: tuple[Lender, str]) -> None:
    state, base = lender
    body = _audio(3_000_000)
    state.files[SID] = body
    state.drip_s = 0.05
    transfers = _transfers(tmp_path, base)
    transfers.observe(EPOCH_A, "joiner", [_load(1, "inviter", 1, body)])
    deadline = time.monotonic() + 10
    while state.served < 200_000 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert any(p.name == "audio.part" for p in _booth_files(tmp_path))
    transfers.observe(EPOCH_A, "joiner", [_end(2)])
    assert _booth_files(tmp_path) == []


# ---------------------------------------------------------------- owned, local and lent


def test_a_track_this_laptop_owns_is_never_transferred(tmp_path: Path, lender: tuple[Lender, str]) -> None:
    state, base = lender
    body = _audio(300_000)
    state.files[SID] = body
    transfers = _transfers(tmp_path, base, owned={hashlib.sha256(body).hexdigest(): "my-own-copy"})
    transfers.observe(EPOCH_A, "joiner", [_load(1, "inviter", 4, body)])
    assert _wait(transfers, 4) == {"state": "owned", "stable_id": "my-own-copy", "reason": None}
    assert [r for r in state.requests if "/audio" in r] == []
    assert _booth_files(tmp_path) == []


def test_my_own_load_plays_locally_and_is_lent_to_the_partner(tmp_path: Path, lender: tuple[Lender, str]) -> None:
    state, base = lender
    body = _audio(300_000)
    transfers = _transfers(tmp_path, base)
    assert not transfers.lends(SID)
    transfers.observe(EPOCH_A, "inviter", [_load(1, "inviter", 2, body)])
    assert transfers.deck_resolution(2) == {"state": "local", "stable_id": SID, "reason": None}
    assert transfers.lends(SID)
    assert not transfers.lends("someone-else")
    assert state.requests == []
    transfers.observe(EPOCH_A, "inviter", [_end(2)])
    assert not transfers.lends(SID), "the lend ends with the set"


def test_the_lender_applying_the_commit_late_is_retried_not_failed(tmp_path: Path, lender: tuple[Lender, str]) -> None:
    state, base = lender
    body = _audio(200_000)
    state.files[SID] = body
    state.not_found_until = time.monotonic() + 0.4
    transfers = _transfers(tmp_path, base)
    transfers.observe(EPOCH_A, "joiner", [_load(1, "inviter", 1, body)])
    assert _wait(transfers, 1)["state"] == "ready"


def test_a_track_the_partner_never_lends_reads_not_found(
    tmp_path: Path, lender: tuple[Lender, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _state, base = lender
    monkeypatch.setattr(bt.CFG, "GRANT_RACE_S", 0.3)
    body = _audio(200_000)
    transfers = _transfers(tmp_path, base)
    transfers.observe(EPOCH_A, "joiner", [_load(1, "inviter", 1, body)])
    res = _wait(transfers, 1)
    assert res == {"state": "failed", "stable_id": None, "reason": "not found"}


def test_ops_out_of_order_are_refused(tmp_path: Path, lender: tuple[Lender, str]) -> None:
    _state, base = lender
    transfers = _transfers(tmp_path, base)
    with pytest.raises(ValueError, match="in order"):
        transfers.observe(EPOCH_A, "joiner", [_end(2)])


# ---------------------------------------------------------------- pacing (an application-level rLEDBAT window)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _pacer() -> tuple[bt.Pacer, _Clock]:
    clock = _Clock()
    return bt.Pacer(enabled=True, clock=clock), clock


def _fill(pacer: bt.Pacer) -> None:
    """The streams use the whole window (window-limited), then the bytes arrive."""
    pacer.acquire(pacer.window)
    pacer.release(pacer.window)


def test_the_window_shrinks_when_queueing_delay_persists_and_grows_below() -> None:
    pacer, _clock = _pacer()
    for _ in range(5):
        pacer.on_rtt(10.0)  # idle base
    start = pacer.window
    assert start == bt.CFG.WINDOW_START_BYTES, "no change while no transfer is active"
    pacer.begin()
    high = 10.0 + bt.CFG.TARGET_QDELAY_MS * 3
    pacer.on_rtt(high)
    assert pacer.window == start and pacer.cuts == 0, "one sample over the target may be a lost probe"
    pacer.on_rtt(high)
    assert pacer.window < start and pacer.cuts == 1, "two in a row are a queue"
    for _ in range(3):
        pacer.on_rtt(high)
    assert pacer.window < start * 0.75 and pacer.cuts == 4
    low = pacer.window
    pacer.on_rtt(11.0)
    for _ in range(10):
        _fill(pacer)
        pacer.on_rtt(11.0)
    assert pacer.window > low * 1.5
    pacer.end()


def test_isolated_retransmit_spikes_never_cut_the_window() -> None:
    """At 1% loss a probe now and then waits out a TCP retransmit (hundreds of ms); that is
    loss, not a queue, and cutting on it starved the transfer (rig round 4)."""
    pacer, _clock = _pacer()
    pacer.on_rtt(50.0)
    pacer.begin()
    for i in range(60):
        _fill(pacer)
        pacer.on_rtt(600.0 if i % 7 == 3 else 51.0)
    assert pacer.cuts == 0 and pacer.window > bt.CFG.WINDOW_START_BYTES * 2
    pacer.end()


def test_the_window_holds_exactly_at_the_target() -> None:
    pacer, _clock = _pacer()
    pacer.on_rtt(10.0)
    pacer.begin()
    for _ in range(bt.CFG.DELAY_FILTER):
        _fill(pacer)
        pacer.on_rtt(10.0 + bt.CFG.TARGET_QDELAY_MS)
    held = pacer.window
    for _ in range(5):
        _fill(pacer)
        pacer.on_rtt(10.0 + bt.CFG.TARGET_QDELAY_MS)
    assert pacer.window == held and pacer.cuts == 0


def test_the_window_grows_only_while_the_streams_fill_it() -> None:
    """Congestion window validation (RFC 7661): a loss-limited or idle transfer that does not
    use its window must not inflate it, or the first loss-free moment floods the queue."""
    pacer, _clock = _pacer()
    pacer.on_rtt(10.0)
    pacer.begin()
    for _ in range(10):
        half = pacer.window // 2
        pacer.acquire(half)
        pacer.release(half)
        pacer.on_rtt(10.0)
    assert pacer.window == bt.CFG.WINDOW_START_BYTES, "half-used: never grown"
    for _ in range(5):
        _fill(pacer)
        pacer.on_rtt(10.0)
    assert pacer.window > bt.CFG.WINDOW_START_BYTES, "filled: grown"
    pacer.end()


def test_the_window_never_passes_its_maximum() -> None:
    pacer, _clock = _pacer()
    pacer.on_rtt(10.0)
    pacer.begin()
    for _ in range(400):
        _fill(pacer)
        pacer.on_rtt(10.0)
    assert pacer.window == bt.CFG.WINDOW_MAX_BYTES


def test_acquire_blocks_past_the_window_and_release_unblocks_it() -> None:
    pacer, _clock = _pacer()
    pacer.window = 100_000
    pacer.acquire(80_000)
    got = threading.Event()
    waiter = threading.Thread(target=lambda: (pacer.acquire(40_000), got.set()), daemon=True)
    waiter.start()
    assert not got.wait(0.3), "a grant past the window waits"
    pacer.release(30_000)
    assert got.wait(2.0), "released bytes make room"
    assert pacer.outstanding == 90_000
    pacer.release(90_000)
    assert pacer.outstanding == 0
    pacer.acquire(500_000)
    assert pacer.outstanding == 500_000, "with nothing outstanding one grant always proceeds (no deadlock)"


def test_acquire_gives_up_on_cancel() -> None:
    pacer, _clock = _pacer()
    pacer.window = 10
    pacer.acquire(10)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(PeerUsbError) as gone:
        pacer.acquire(10, cancel)
    assert gone.value.code == "VUSB_PEER_UNREACHABLE"


def test_chunk_size_splits_the_window_across_streams_within_bounds() -> None:
    pacer, _clock = _pacer()
    pacer.window = 16 * 300_000
    assert pacer.chunk_bytes(16) == 300_000
    pacer.window = 1
    assert pacer.chunk_bytes(16) == bt.CFG.CHUNK_MIN_BYTES
    pacer.window = 10**9
    assert pacer.chunk_bytes(16) == bt.CFG.CHUNK_MAX_BYTES


def test_the_base_round_trip_is_the_lowest_of_the_recent_minutes() -> None:
    pacer, clock = _pacer()
    pacer.on_rtt(8.0)
    clock.t += 60 * (bt.CFG.BASE_HISTORY_MIN + 1)
    pacer.on_rtt(30.0)
    assert pacer.base_rtt_ms() == 30.0, "a minimum older than the base history is forgotten"
    pacer.on_rtt(25.0)
    assert pacer.base_rtt_ms() == 25.0


def test_a_disabled_pacer_never_changes_its_window_and_is_never_used_to_gate() -> None:
    clock = _Clock()
    pacer = bt.Pacer(enabled=False, clock=clock)
    pacer.on_rtt(10.0)
    pacer.begin()
    for _ in range(5):
        pacer.on_rtt(200.0)
    assert pacer.window == bt.CFG.WINDOW_START_BYTES
    assert pacer.status()["mode"] == "off"
    assert pacer.status()["qdelay_ms"] == 190.0, "the instrument still measures with pacing off"
    assert (
        bt.BoothTransfers(
            Path("/nonexistent"),
            peer=lambda: ("", {}),
            find_owned=lambda *_: None,
            peer_lender_id=lambda: LENDER_ID,
            pacing=False,
            probe=False,
        )._window()
        is None
    )


def test_the_pacer_refuses_a_bad_round_trip() -> None:
    with pytest.raises(ValueError, match="finite"):
        bt.Pacer(enabled=True).on_rtt(float("nan"))


class _RecordingWindow(bt.Pacer):
    """The real window, recording the most bytes ever outstanding."""

    def __init__(self) -> None:
        super().__init__(enabled=True)
        self.window = 300_000
        self.peak = 0

    def acquire(self, nbytes: int, cancel: threading.Event | None = None) -> None:
        super().acquire(nbytes, cancel)
        self.peak = max(self.peak, self.outstanding)


def test_a_windowed_download_never_has_more_requested_than_the_window(
    tmp_path: Path, lender: tuple[Lender, str]
) -> None:
    state, base = lender
    body = _audio(4_000_003)
    state.files[SID] = body
    window = _RecordingWindow()
    root = bt.booth_root(tmp_path)
    peer = lambda: (f"{base}/api/v1/b2b/usb/export/tracks/{SID}/audio", {})  # noqa: E731
    audio = cache.ensure_ranged_audio(
        root,
        LENDER_ID,
        SID,
        peer=peer,
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        max_bytes=10**9,
        control=cache.FetchControl(streams=16, window=window),
    )
    assert audio.path.read_bytes() == body
    assert 0 < window.peak <= max(window.window, window.chunk_bytes(16)), window.peak
    assert window.outstanding == 0, "every granted byte is released"
    assert len([r for r in state.requests if "/audio" in r]) > 16, "requests are sub-ranges of a window chunk"


# ---------------------------------------------------------------- own library by sha256


def _library(tmp_path: Path, rows: list[tuple[str, str | None, Path, str | None]]) -> Path:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, content_hash TEXT, file_path TEXT, deleted_at TEXT)")
    conn.executemany("INSERT INTO tracks VALUES (?, ?, ?, ?)", [(sid, h, str(p), d) for sid, h, p, d in rows])
    conn.commit()
    conn.close()
    return db


def test_library_owners_reads_the_stored_hash_first(tmp_path: Path) -> None:
    body = _audio(1000)
    sha = hashlib.sha256(body).hexdigest()
    f = tmp_path / "a.mp3"
    f.write_bytes(body)
    db = _library(
        tmp_path, [("s1", f"sha256:{sha}", f, None), ("s2", sha, f, None), ("s3", f"sha256:{sha}", f, "2026-01-01")]
    )
    assert bt.library_owners(db, sha, len(body), tmp_path / "h.db") == ["s1", "s2"]
    assert bt.library_owners(db, "0" * 64, len(body), tmp_path / "h.db") == []


def test_library_owners_hashes_only_unhashed_files_of_the_same_size(tmp_path: Path) -> None:
    body = _audio(1000)
    sha = hashlib.sha256(body).hexdigest()
    same, other = tmp_path / "same.mp3", tmp_path / "other.mp3"
    same.write_bytes(body)
    other.write_bytes(_audio(999, seed=2))
    hcache = tmp_path / "h.db"
    db = _library(tmp_path, [("x-same", None, same, None), ("y-other", None, other, None)])
    assert bt.library_owners(db, sha, len(body), hcache) == ["x-same"]
    hashed = [row[0] for row in sqlite3.connect(hcache).execute("SELECT path FROM file_hashes")]
    assert hashed == [str(same)], "a file of another size is never hashed"


def test_library_owners_without_a_library_is_none(tmp_path: Path) -> None:
    assert bt.library_owners(tmp_path / "missing.db", "0" * 64, 1, tmp_path / "h.db") == []


# ---------------------------------------------------------------- separate caches


def test_booth_reservations_never_count_against_the_borrowed_usb_cache(tmp_path: Path) -> None:
    booth = bt.booth_root(tmp_path)
    usb = cache.cache_root(tmp_path)
    entry = cache.entry_dir(booth, LENDER_ID, SID)
    with cache._admitted(booth, entry, 10_000, 20_000):
        with cache._RESERVE_GUARD:
            assert cache._reserved_remaining(usb, exclude=None) == 0
            assert cache._reserved_remaining(booth, exclude=None) > 0
        assert cache.clear(usb) == 0, "the borrowed-USB cache clears while a booth transfer is in flight"
        with pytest.raises(PeerUsbError) as busy:
            cache.clear(booth)
        assert busy.value.code == "VUSB_DOWNLOAD_IN_PROGRESS"


def test_a_sidecar_with_shifted_boundaries_is_not_trusted(tmp_path: Path) -> None:
    # Same size, same range count, other edges: resuming onto it would write each range's
    # kept bytes at the wrong offsets (mutation round: the bounds check alone let it through).
    part = tmp_path / "audio.part"
    part.write_bytes(b"\0" * 100)
    cache._save_ranges(
        part,
        100,
        [cache._Range(0, 9, 5), cache._Range(10, 49, 5), cache._Range(50, 74, 5), cache._Range(75, 99, 5)],
        "audio/mpeg",
    )
    ranges, media = cache._load_ranges(part, 100, 4)
    assert [(r.start, r.end, r.done) for r in ranges] == [(0, 24, 0), (25, 49, 0), (50, 74, 0), (75, 99, 0)]
    assert media is None


def test_broken_streams_release_their_window_grant(tmp_path: Path, lender: tuple[Lender, str]) -> None:
    # A stream cut mid-request must give back the bytes it was granted and never received, or
    # the window leaks shut and the transfer deadlocks (mutation round finding).
    state, base = lender
    body = _audio(4_000_003)
    state.files[SID] = body
    state.cut_after = 30_000
    window = _RecordingWindow()
    cancel = threading.Event()
    timer = threading.Timer(30.0, cancel.set)
    timer.start()
    try:
        audio = cache.ensure_ranged_audio(
            bt.booth_root(tmp_path),
            LENDER_ID,
            SID,
            peer=lambda: (f"{base}/api/v1/b2b/usb/export/tracks/{SID}/audio", {}),
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            max_bytes=10**9,
            control=cache.FetchControl(streams=16, window=window, cancel=cancel),
        )
    finally:
        timer.cancel()
    assert audio.path.read_bytes() == body
    assert window.outstanding == 0


class _FakeBooth:
    """The engine booth's committed log as ``sync_from`` reads it."""

    def __init__(self, epoch: str | None, ops: list[dict[str, Any]], side: str = "joiner") -> None:
        self.epoch, self.ops, self.side = epoch, ops, side
        self.reads: list[int] = []

    def log_since(self, seq: int) -> dict[str, Any]:
        self.reads.append(seq)
        if self.epoch is None:
            return {"epoch": None, "seq": 0, "ops": []}
        return {"epoch": self.epoch, "seq": len(self.ops), "ops": self.ops[seq:]}


def test_sync_from_hands_over_only_new_ops_rereads_on_a_new_epoch_and_closes_with_the_booth(
    tmp_path: Path, lender: tuple[Lender, str]
) -> None:
    _l, base = lender
    transfers = _transfers(tmp_path, base)
    body = _audio(200_000)
    mine = _FakeBooth("e1", [_load(1, "joiner", 3, body)])
    transfers.sync_from(mine)
    assert (transfers.epoch, transfers.seen_seq) == ("e1", 1) and transfers.lends(SID)
    mine.ops.append(_load(2, "joiner", 4, body, sid="other1"))
    transfers.sync_from(mine)
    assert mine.reads[-1] == 1, "only the ops since the last one seen"
    assert transfers.seen_seq == 2 and transfers.lends("other1")
    restarted = _FakeBooth("e2", [_load(1, "joiner", 1, body, sid="third1")])
    transfers.sync_from(restarted)
    assert restarted.reads == [2, 0], "a new epoch re-reads its log from the start"
    assert transfers.epoch == "e2" and transfers.lends("third1") and not transfers.lends(SID), "the old epoch is wiped"
    transfers.sync_from(_FakeBooth(None, []))
    assert transfers.epoch is None and transfers.last_wipe is not None and transfers.last_wipe["reason"] == "booth closed"
    wipes = transfers.wipes
    transfers.sync_from(_FakeBooth(None, []))
    assert transfers.wipes == wipes, "a closed booth read again is not another wipe"
