"""A one-laptop Link session behind Carabiner's line protocol, for in-process relay tests.

It speaks the subset the relay and ``CarabinerClient`` use (``status``, ``bpm``,
``force-beat-at-time``, ``request-beat-at-time``) with Link's own timeline math on the real Link
host clock (``link_now_us``): a tempo change keeps the current beat, a force maps the beat to the
time exactly. The real Carabiner is exercised by the netns e2e (``tests/b2b_e2e``); this stands
in for it where two sessions on one host must be kept apart without a namespace.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Callable

from apps.ableton_link.link_clock import beat_at_time, link_now_us


class SimCarabiner:
    def __init__(
        self,
        bpm: float = 128.0,
        peers: int = 0,
        start_us: float | None = None,
        port: int = 0,
        now_us: Callable[[], float] = link_now_us,
    ) -> None:
        self.bpm = bpm
        self.peers = peers
        self._now_us = now_us
        self.start_us = float(now_us() if start_us is None else start_us)
        self.commands: list[str] = []
        self._lock = threading.Lock()
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", port))  # a fixed port stands in for a relaunched Carabiner
        self._server.listen(4)
        self.port = int(self._server.getsockname()[1])
        self._conns: list[socket.socket] = []
        #: Client connections accepted so far (a re-dial shows up here).
        self.accepted = 0
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def beat_now(self) -> float:
        with self._lock:
            return beat_at_time(self.start_us, self.bpm, self._now_us())

    def status_line(self) -> bytes:
        with self._lock:
            beat = beat_at_time(self.start_us, self.bpm, self._now_us())
            return (f"status {{ :peers {self.peers} :bpm {self.bpm:.6f} :start {int(self.start_us)} "
                    f":beat {beat:.6f} }}\n").encode()

    def push(self) -> None:
        line = self.status_line()
        for conn in list(self._conns):
            with contextlib.suppress(OSError):  # a closed client is not this sim's concern
                conn.sendall(line)

    def set_tempo(self, bpm: float) -> None:
        """What a DJ app does on this laptop's session: keep the beat, change the tempo."""
        with self._lock:
            now = self._now_us()
            beat = beat_at_time(self.start_us, self.bpm, now)
            self.bpm = bpm
            self.start_us = now - beat * 60e6 / bpm
        self.push()

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            self._conns.append(conn)
            self.accepted += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        buffer = b""
        while not self._closed:
            try:
                chunk = conn.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                self._command(conn, raw.decode().strip())

    def _command(self, conn: socket.socket, line: str) -> None:
        self.commands.append(line)
        parts = line.split()
        if not parts:
            return
        if parts[0] == "bpm":
            self.set_tempo(float(parts[1]))
            return
        if parts[0] in ("force-beat-at-time", "request-beat-at-time"):
            beat, at_us = float(parts[1]), float(parts[2])
            with self._lock:
                self.start_us = at_us - beat * 60e6 / self.bpm
        conn.sendall(self.status_line())

    def close(self) -> None:
        self._closed = True
        for conn in self._conns:
            conn.close()
        # shutdown wakes the accept thread so the port is free to rebind at once (Linux keeps a
        # listening socket alive while a thread is blocked in accept on it).
        with contextlib.suppress(OSError):
            self._server.shutdown(socket.SHUT_RDWR)
        self._server.close()
