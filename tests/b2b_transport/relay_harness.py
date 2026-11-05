"""A REAL local relay (``wrangler dev --local``: workerd running ``cloud--b2b-relay``) and a
wire tap in front of it that records every byte the relay receives and sends.

Nothing here imitates the relay: the Worker and its Durable Objects run in workerd exactly as
they would on Cloudflare, minus the network. The tap is a plain TCP forwarder; ``frames``
decodes the WebSocket frames it saw (unmasking the client side), so a test can assert what
the relay could read.
"""

from __future__ import annotations

import contextlib
import os
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
RELAY_DIR = REPO / "cloud--b2b-relay"
WRANGLER = RELAY_DIR / "node_modules" / ".bin" / "wrangler"
#: The Worker config; a runner can keep a stale ``node_modules`` from another branch without it.
WORKER_CONFIG = RELAY_DIR / "wrangler.jsonc"
BOOT_BUDGET_S = 90.0
UNAVAILABLE = (f"UNAVAILABLE: {WORKER_CONFIG} or {WRANGLER} is missing (run `pnpm install` in cloud--b2b-relay), so the real "
               "local relay cannot run. Not a pass.")
#: CI sets this to 1 where it installed the relay: there a missing wrangler is a failure, not a skip.
REQUIRE_RELAY_ENV = "MDT_REQUIRE_B2B_RELAY"


def relay_available() -> bool:
    """Both the Worker source and its wrangler are present, so ``wrangler dev`` can boot it.

    POSIX only: teardown stops the relay process group with ``os.killpg``."""
    return os.name == "posix" and WORKER_CONFIG.exists() and WRANGLER.exists()


def relay_or_skip() -> pytest.MarkDecorator:
    """Skip a relay test when the Worker or wrangler is missing, unless ``MDT_REQUIRE_B2B_RELAY=1`` makes that a failure."""
    if not relay_available() and os.environ.get(REQUIRE_RELAY_ENV) == "1":
        raise RuntimeError(f"{UNAVAILABLE} {REQUIRE_RELAY_ENV}=1 is set, so this is a failure.")
    return pytest.mark.skipif(not relay_available(), reason=UNAVAILABLE)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def local_relay(state_dir: Path, relay_vars: dict[str, str] | None = None, ip: str = "127.0.0.1") -> Iterator[str]:
    """``wrangler dev --local`` on a free port; yields its base URL once /v1/health answers.

    ``relay_vars`` override ``wrangler.jsonc`` vars (``--var KEY:VALUE``), e.g. a short
    ``INVITE_TTL_MS`` for an expiry test. ``ip`` is the address it binds (loopback by default)."""
    port = free_port()
    overrides = [arg for key, value in (relay_vars or {}).items() for arg in ("--var", f"{key}:{value}")]
    log_path = state_dir / "wrangler.log"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "WRANGLER_SEND_METRICS": "false", "CI": "1", "NO_COLOR": "1",
           "AF_SERVICE_ID": "com.opendj.b2b-relay-test"}
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            [str(WRANGLER), "dev", "--local", "--ip", ip, "--port", str(port),
             "--persist-to", str(state_dir / "persist"), "--show-interactive-dev-session=false", *overrides],
            cwd=RELAY_DIR, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    base = f"http://{'127.0.0.1' if ip == '0.0.0.0' else ip}:{port}"
    try:
        deadline = time.monotonic() + BOOT_BUDGET_S
        while True:
            assert proc.poll() is None, f"wrangler dev exited on boot; see {log_path}:\n{log_path.read_text()[-3000:]}"
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{base}/v1/health", timeout=1.0).json() == {"ok": True, "v": 1}:
                    break
            assert time.monotonic() < deadline, f"local relay did not answer /v1/health in {BOOT_BUDGET_S}s; see {log_path}"
            time.sleep(0.5)
        yield base
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, 15)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            proc.wait()


# ---------------------------------------------------------------- the wire tap


@dataclass
class Capture:
    """Raw bytes per connection and direction: ``up`` = client to relay, ``down`` = relay to client."""

    up: list[bytearray] = field(default_factory=list)
    down: list[bytearray] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def all_bytes(self) -> bytes:
        with self.lock:
            return b"".join(bytes(b) for b in self.up + self.down)

    def frames(self) -> list[tuple[str, int, bytes]]:
        """Every WebSocket frame on the wire as (direction, opcode, unmasked payload)."""
        out: list[tuple[str, int, bytes]] = []
        with self.lock:
            streams = [("up", bytes(b)) for b in self.up] + [("down", bytes(b)) for b in self.down]
        for direction, raw in streams:
            head_end = raw.find(b"\r\n\r\n")
            if head_end < 0 or b"upgrade: websocket" not in raw[:head_end].lower():
                continue  # plain HTTP (the nameplate POST): its head is in ``readable``, its body is not a frame
            out.extend((direction, opcode, payload) for opcode, payload in _decode_frames(raw[head_end + 4:]))
        return out

    def readable(self) -> bytes:
        """Everything the relay could read: HTTP heads plus every unmasked frame payload."""
        heads = b""
        with self.lock:
            for raw in (bytes(b) for b in self.up + self.down):
                heads += raw[: max(raw.find(b"\r\n\r\n"), 0)]
        return heads + b"".join(payload for _, _, payload in self.frames())


def _decode_frames(data: bytes) -> Iterator[tuple[int, bytes]]:
    offset = 0
    while offset + 2 <= len(data):
        b0, b1 = data[offset], data[offset + 1]
        opcode, masked, length = b0 & 0x0F, b1 & 0x80, b1 & 0x7F
        # A compressed frame (RSV1, permessage-deflate) is unreadable to this decoder: say so
        # instead of returning bytes that would make a plaintext search come up empty.
        assert not b0 & 0x40, "a compressed WebSocket frame crossed the tap; it cannot judge what the relay reads"
        offset += 2
        if length == 126:
            (length,) = struct.unpack(">H", data[offset:offset + 2])
            offset += 2
        elif length == 127:
            (length,) = struct.unpack(">Q", data[offset:offset + 8])
            offset += 8
        mask = data[offset:offset + 4] if masked else b""
        offset += 4 if masked else 0
        payload = data[offset:offset + length]
        offset += length
        if len(payload) < length:
            return  # a frame cut off by the end of the capture
        yield opcode, bytes(p ^ mask[i % 4] for i, p in enumerate(payload)) if masked else payload


class WireTap:
    """A loopback TCP forwarder to ``target``; ``capture`` holds every byte both ways."""

    def __init__(self, target: str) -> None:
        host_port = target.split("://", 1)[1]
        host, port = host_port.split(":")
        self._target = (host, int(port))
        self.capture = Capture()
        self._server = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._server.getsockname()[1]}"
        self._stop = threading.Event()
        threading.Thread(target=self._accept, name="wire-tap", daemon=True).start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            upstream = socket.create_connection(self._target)
            up, down = bytearray(), bytearray()
            with self.capture.lock:
                self.capture.up.append(up)
                self.capture.down.append(down)
            threading.Thread(target=self._pump, args=(client, upstream, up), daemon=True).start()
            threading.Thread(target=self._pump, args=(upstream, client, down), daemon=True).start()

    def _pump(self, source: socket.socket, sink: socket.socket, record: bytearray) -> None:
        try:
            while chunk := source.recv(65536):
                with self.capture.lock:
                    record.extend(chunk)
                sink.sendall(chunk)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                sink.shutdown(socket.SHUT_WR)

    def close(self) -> None:
        self._stop.set()
        self._server.close()


class WebSocketBlocker:
    """A loopback HTTP forwarder that passes plain requests to ``target`` and answers every
    WebSocket upgrade with 502, the way a proxy that does not speak WebSocket does. ``bodies``
    keeps every response it forwarded so a test can read what the relay answered."""

    def __init__(self, target: str) -> None:
        self._target = target.rstrip("/")
        self.bodies: list[bytes] = []
        self.blocked = 0
        self._server = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._server.getsockname()[1]}"
        threading.Thread(target=self._accept, name="ws-blocker", daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        with client:
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = client.recv(65536)
                if not chunk:
                    return
                raw += chunk
            head, _, rest = raw.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            if any(k.lower() == "upgrade" and v.lower() == "websocket" for k, v in headers.items()):
                self.blocked += 1
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")
                return
            length = int(next((v for k, v in headers.items() if k.lower() == "content-length"), "0"))
            while len(rest) < length:
                rest += client.recv(65536)
            response = httpx.request(method, f"{self._target}{path}", content=rest, timeout=10)
            self.bodies.append(response.content)
            client.sendall(f"HTTP/1.1 {response.status_code} X\r\ncontent-length: {len(response.content)}\r\n"
                           "connection: close\r\n\r\n".encode() + response.content)

    def close(self) -> None:
        self._server.close()
