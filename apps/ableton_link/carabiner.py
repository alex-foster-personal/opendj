"""A client for Carabiner's line protocol (Deep Symmetry, GPLv2, out of process).

Carabiner joins the Ableton Link session on the LAN and serves it on a TCP
port (17000 by default). It answers ``status`` with one line::

    status { :peers 1 :bpm 128.000000 :start 73746356220 :beat 597.737570 }

and sends the same line unprompted whenever the session tempo or the peer
count changes. ``bpm <tempo>`` proposes a new session tempo to every peer.
LINK-10 and LINK-11 need tempo and peer count. LINK-12+ (B2B phase) also
read ``:start`` (the Link time of beat 0) and drive ``request-beat-at-time`` /
``force-beat-at-time``, which Carabiner answers with a fresh status line (or a
``bad-*`` line naming the argument it refused).

Nothing here talks Link multicast itself and nothing here links the Link SDK:
the GPL code stays inside the separate Carabiner process (LINK-05, D1).
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

#: Carabiner's own default port (its ``--port`` flag moves it).
DEFAULT_CARABINER_PORT: int = 17000
CARABINER_PORT_ENV: str = "MDT_CARABINER_PORT"

#: How often the reader asks for ``status`` when Carabiner has said nothing.
#: Carabiner pushes changes unprompted; this is the liveness check.
POLL_INTERVAL_S: float = 1.0
#: No line at all for this long means the sidecar is gone.
STALE_AFTER_S: float = 3.0
#: LINK-118: a status line whose tempo moved more than this is a session tempo change.
SESSION_BPM_EPS: float = 1e-4
#: LINK-118: a status line whose ``:start`` moved more than this is a timeline move (a forced beat
#: or a tempo change). Link re-measures its host-time mapping all the time, which re-rounds
#: ``:start`` by microseconds; that is not a move.
SESSION_START_EPS_US: float = 500.0
CONNECT_TIMEOUT_S: float = 1.0
#: How long a command waits for Carabiner's status reply.
REPLY_TIMEOUT_S: float = 1.0

#: LINK-121: after Carabiner drops, the reader re-dials on this schedule (seconds before
#: attempt 1, 2, 3, then every last value), for as long as Link stays joined. The cap keeps a
#: KeepAlive relaunch (about 1-2 s) seen within one cap while never hammering a dead port.
REDIAL_BACKOFF_S: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0)
#: The plain words every surface (route error, CLI, mirror, LINK hover title) shows meanwhile.
REJOINING: str = "Carabiner restarting, rejoining"
#: Failed re-dials logged one by one up to this many, then every REDIAL_LOG_EVERY-th.
REDIAL_LOG_FIRST: int = 3
REDIAL_LOG_EVERY: int = 15


#: Ableton Link's own tempo bounds.
MIN_LINK_BPM: float = 20.0
MAX_LINK_BPM: float = 999.0

_STATUS = re.compile(r"^status\s*\{(?P<body>.*)\}\s*$")
_FIELD = re.compile(r":(?P<key>[a-z][a-z-]*)\s+(?P<value>[^\s}]+)")


class CarabinerUnavailable(RuntimeError):
    """Carabiner could not be reached or stopped answering. The message says why."""


@dataclass(frozen=True)
class LinkSnapshot:
    """The last session state Carabiner reported."""

    connected: bool
    peers: int | None
    bpm: float | None
    beat: float | None
    #: Seconds since the last line from Carabiner, or None before the first.
    age_s: float | None
    error: str | None
    #: Link host time (us) of session beat 0, from ``:start``.
    start_us: float | None = None
    #: ``:playing`` once start/stop sync is on; None while it is off.
    playing: bool | None = None
    #: B2BMCAST-01: seconds the peer count has read 0 without a break, measured by the reader
    #: thread's own ``status`` polling (not by whoever asks); None while peers > 0 or unknown.
    zero_peers_for_s: float | None = None
    #: Seconds since the first status line on this connection; None before it.
    connected_for_s: float | None = None
    #: LINK-121: Carabiner dropped while joined and the reader is re-dialing it.
    rejoining: bool = False


def redial_delay_s(attempt: int) -> float:
    """Seconds to wait before re-dial ``attempt`` (1-based): the capped LINK-121 back-off."""
    return REDIAL_BACKOFF_S[min(attempt, len(REDIAL_BACKOFF_S)) - 1]


def carabiner_port() -> int:
    """The port to dial: ``MDT_CARABINER_PORT`` or Carabiner's default."""
    raw = os.environ.get(CARABINER_PORT_ENV)
    if raw is None or raw == "":
        return DEFAULT_CARABINER_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{CARABINER_PORT_ENV} must be an integer port, got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{CARABINER_PORT_ENV} must be 1..65535, got {port}")
    return port


def parse_status_line(line: str) -> dict[str, float | int | bool] | None:
    """Parse one ``status { ... }`` line; None for any other line.

    Raises ``ValueError`` for a status line whose peers or bpm will not parse,
    because a half-read status must not render as a tempo.
    """
    match = _STATUS.match(line.strip())
    if match is None:
        return None
    fields: dict[str, float | int | bool] = {}
    for item in _FIELD.finditer(match.group("body")):
        key, raw = item.group("key"), item.group("value")
        if raw in ("true", "false"):
            fields[key] = raw == "true"
        elif key == "peers":
            fields[key] = int(raw)
        else:
            fields[key] = float(raw)
    if "peers" not in fields or "bpm" not in fields:
        raise ValueError(f"Carabiner status line without peers and bpm: {line.strip()!r}")
    bpm = fields["bpm"]
    if not isinstance(bpm, float) or not math.isfinite(bpm) or bpm <= 0:
        raise ValueError(f"Carabiner status line with an unusable bpm: {line.strip()!r}")
    return fields


_LOG = logging.getLogger(__name__)


class CarabinerClient:
    """One TCP connection to Carabiner, read on a daemon thread.

    ``start`` dials and fails loud (``CarabinerUnavailable``) when nothing is
    listening; after that, ``snapshot`` never blocks and reports ``connected``
    False with the reason once the sidecar goes away.
    """

    def __init__(
        self, host: str = "127.0.0.1", port: int | None = None, on_session_change: Callable[[], None] | None = None
    ) -> None:
        self.host = host
        #: LINK-118: called (outside the lock, on the reader thread) when a status line moves the
        #: session tempo or timeline, so the owner can tell the page at once.
        self._on_session_change = on_session_change
        self.port = carabiner_port() if port is None else port
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._peers: int | None = None
        self._bpm: float | None = None
        self._beat: float | None = None
        self._start_us: float | None = None
        self._playing: bool | None = None
        self._last_line_at: float | None = None
        self._first_line_at: float | None = None
        self._zero_peers_since: float | None = None
        self._error: str | None = None
        #: Bumped on every status line; a command waits for it to move.
        self._status_seq = 0
        self._reply_error: str | None = None
        self._reply = threading.Condition(self._lock)
        #: LINK-121 re-dial state: monotonic time Carabiner dropped, attempts so far, and why.
        self._rejoin_since: float | None = None
        self._rejoin_attempts = 0
        self._rejoin_reason: str | None = None
        self._connected_at: float | None = None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT_S)
        except OSError as exc:
            raise CarabinerUnavailable(
                f"Carabiner is not answering on {self.address} ({exc.strerror or exc}). "
                "Start Carabiner (Deep Symmetry) on this machine, or set "
                f"{CARABINER_PORT_ENV} to the port it listens on."
            ) from exc
        sock.settimeout(POLL_INTERVAL_S)
        self._stop.clear()
        self._sock = sock
        self._error = None
        with self._lock:  # a new connection measures its own streaks
            self._first_line_at = None
            self._zero_peers_since = None
            self._connected_at = time.monotonic()
        self._send("status")
        self._thread = threading.Thread(target=self._read_loop, name="carabiner-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Leave Link: no re-dial follows (the user's intent is off)."""
        with self._lock:  # a re-dial installs its socket under this lock, after checking _stop
            self._stop.set()
            sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def chain_session_change(self, extra: Callable[[], None]) -> Callable[[], None]:
        """Run ``extra`` after any existing session-change callback; return ``restore`` to undo only this link.

        ``_handle`` calls a single delegate; assigning ``_on_session_change`` replaces it. Callers that
        need both their hook and a prior one must chain here instead of overwriting the attribute.
        """
        prior = self._on_session_change

        def combined() -> None:
            if prior is not None:
                prior()
            extra()

        self._on_session_change = combined

        def restore() -> None:
            if self._on_session_change is combined:
                self._on_session_change = prior

        return restore

    def set_bpm(self, bpm: float) -> None:
        """Propose ``bpm`` as the session tempo to every Link peer."""
        if not math.isfinite(bpm) or not MIN_LINK_BPM <= bpm <= MAX_LINK_BPM:
            raise ValueError(f"Link tempo must be {MIN_LINK_BPM:g}..{MAX_LINK_BPM:g} BPM, got {bpm}")
        if self._sock is None:
            raise CarabinerUnavailable("Link is not connected to Carabiner")
        if not self._send(f"bpm {bpm:.6f}"):
            raise CarabinerUnavailable(self.snapshot().error or f"Carabiner on {self.address} refused the tempo")

    def command(self, line: str, timeout_s: float = REPLY_TIMEOUT_S) -> LinkSnapshot:
        """Send a command Carabiner answers with a status line; wait for it.

        Raises ``CarabinerUnavailable`` when the write fails, Carabiner answers
        ``bad-*`` / ``unsupported``, or no status arrives within ``timeout_s``.
        """
        if self._sock is None:
            raise CarabinerUnavailable("Link is not connected to Carabiner")
        with self._lock:
            seq = self._status_seq
            self._reply_error = None
        if not self._send(line):
            raise CarabinerUnavailable(self.snapshot().error or f"Carabiner on {self.address} refused {line!r}")
        deadline = time.monotonic() + timeout_s
        with self._reply:
            while self._status_seq == seq and self._reply_error is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise CarabinerUnavailable(f"Carabiner on {self.address} did not answer {line!r} within {timeout_s:g} s")
                self._reply.wait(left)
            if self._reply_error is not None:
                raise CarabinerUnavailable(f"Carabiner refused {line!r}: {self._reply_error}")
        return self.snapshot()

    def set_beat_at_time(self, beat: float, at_us: int, quantum: float, force: bool) -> LinkSnapshot:
        """``request-beat-at-time`` (polite, phase kept when peers exist) or
        ``force-beat-at-time`` (moves every peer's timeline)."""
        if not (math.isfinite(beat) and math.isfinite(quantum) and quantum > 0):
            raise ValueError(f"beat must be finite and quantum > 0, got beat={beat} quantum={quantum}")
        verb = "force-beat-at-time" if force else "request-beat-at-time"
        return self.command(f"{verb} {beat:.6f} {int(at_us)} {quantum:.6f}")

    def snapshot(self) -> LinkSnapshot:
        with self._lock:
            age = None if self._last_line_at is None else time.monotonic() - self._last_line_at
            connected = self._sock is not None and self._error is None and age is not None and age < STALE_AFTER_S
            error = self._error
            if error is None and self._sock is not None and age is not None and age >= STALE_AFTER_S:
                error = f"Carabiner on {self.address} has not answered for {age:.1f} s"
            now = time.monotonic()
            rejoining = self._rejoin_since is not None
            if rejoining:
                assert self._rejoin_since is not None
                error = (f"{REJOINING} (down {now - self._rejoin_since:.1f} s, {self._rejoin_attempts} "
                         f"dial(s)): {self._rejoin_reason}")
            return LinkSnapshot(
                connected=connected,
                peers=self._peers,
                bpm=self._bpm,
                beat=self._beat,
                age_s=age,
                error=error,
                start_us=self._start_us,
                playing=self._playing,
                zero_peers_for_s=None if self._zero_peers_since is None else round(now - self._zero_peers_since, 2),
                connected_for_s=None if self._first_line_at is None else round(now - self._first_line_at, 2),
                rejoining=rejoining,
            )

    def _send(self, line: str) -> bool:
        """Write one line; False (with the reason recorded) when it could not."""
        sock = self._sock
        if sock is None:
            return False
        try:
            # The reader thread's liveness `status` and a request's `bpm` must
            # not interleave inside one line.
            with self._send_lock:
                sock.sendall((line + "\n").encode("ascii"))
        except OSError as exc:
            self._fail(f"Carabiner on {self.address} refused a write ({exc})")
            return False
        return True

    def _fail(self, reason: str) -> None:
        with self._lock:
            self._error = reason

    def _read_loop(self) -> None:
        """Read until stopped. A drop while joined re-dials (LINK-121) instead of ending."""
        while not self._stop.is_set():
            reason = self._read_until_drop()
            if reason is None or self._stop.is_set():
                return
            if not self._rejoin(reason):
                return

    def _read_until_drop(self) -> str | None:
        """Read the current connection; the drop reason, or None when stopped."""
        buffer = b""
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return None
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                with self._lock:
                    heard = self._last_line_at if self._last_line_at is not None else self._connected_at
                silent = None if heard is None else time.monotonic() - heard
                if silent is not None and silent >= STALE_AFTER_S:  # a hung Carabiner is a dropped one
                    return f"Carabiner on {self.address} has not answered for {silent:.1f} s"
                self._send("status")
                continue
            except OSError as exc:
                return f"Carabiner on {self.address} dropped the connection ({exc})"
            if not chunk:
                return f"Carabiner on {self.address} closed the connection"
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                self._handle(raw.decode("utf-8", errors="replace"))
        return None

    def _rejoin(self, reason: str) -> bool:
        """Re-dial on the capped back-off until connected (True) or stopped (False)."""
        down_at = time.monotonic()
        with self._lock:
            old, self._sock = self._sock, None
            self._rejoin_since, self._rejoin_attempts, self._rejoin_reason = down_at, 0, reason
            self._last_line_at = None
            self._first_line_at = None
            self._zero_peers_since = None
            self._reply.notify_all()
        if old is not None:
            old.close()
        _LOG.warning("Link: %s; %s on %s", reason, REJOINING, self.address)
        attempt = 0
        while not self._stop.wait(redial_delay_s(attempt + 1)):
            attempt += 1
            with self._lock:
                self._rejoin_attempts = attempt
            try:
                sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT_S)
            except OSError as exc:
                if attempt <= REDIAL_LOG_FIRST or attempt % REDIAL_LOG_EVERY == 0:
                    _LOG.info("Link: re-dial %d to %s failed (%s)", attempt, self.address, exc.strerror or exc)
                continue
            sock.settimeout(POLL_INTERVAL_S)
            with self._lock:
                if self._stop.is_set():  # Link was turned off while this dial was in flight
                    sock.close()
                    return False
                self._sock = sock
                self._error = None
                self._connected_at = time.monotonic()
                self._rejoin_since = self._rejoin_reason = None
                self._rejoin_attempts = 0
            self._send("status")
            _LOG.warning("Link: rejoined Carabiner on %s after %.2f s, %d dial(s)",
                         self.address, time.monotonic() - down_at, attempt)
            return True
        return False

    def _handle(self, line: str) -> None:
        try:
            fields = parse_status_line(line)
        except ValueError as exc:
            self._fail(str(exc))
            return
        if self._record_status(line, fields) and self._on_session_change is not None:
            try:
                self._on_session_change()
            except Exception:  # noqa: BLE001 -- any listener failure; logged with its traceback
                # The reader is the only thing keeping the session state fresh: a failing listener
                # is reported loudly, never allowed to stop it (the page's 4 Hz poll still runs).
                _LOG.exception("Carabiner on %s: the session-change listener raised", self.address)

    def _record_status(self, line: str, fields: dict[str, float | bool | int] | None) -> bool:
        """Store one parsed line; True when it moved the session tempo or timeline."""
        with self._lock:
            self._last_line_at = time.monotonic()
            if fields is None:
                stripped = line.strip()
                if stripped.startswith(("bad-", "unsupported")):
                    self._reply_error = stripped
                    self._reply.notify_all()
                return False
            peers, bpm, beat = fields["peers"], fields["bpm"], fields.get("beat")
            start, playing = fields.get("start"), fields.get("playing")
            if int(peers) > 0:
                self._zero_peers_since = None
            elif self._zero_peers_since is None:
                self._zero_peers_since = self._last_line_at
            if self._first_line_at is None:
                self._first_line_at = self._last_line_at
            new_start = float(start) if isinstance(start, float) else None
            moved = (
                self._bpm is None
                or abs(float(bpm) - self._bpm) > SESSION_BPM_EPS
                or (new_start is None) != (self._start_us is None)
                or (
                    new_start is not None
                    and self._start_us is not None
                    and abs(new_start - self._start_us) > SESSION_START_EPS_US
                )
            )
            self._peers = int(peers)
            self._bpm = float(bpm)
            self._beat = float(beat) if isinstance(beat, float) else None
            self._start_us = new_start
            self._playing = playing if isinstance(playing, bool) else None
            self._error = None
            self._status_seq += 1
            self._reply.notify_all()
            return moved
