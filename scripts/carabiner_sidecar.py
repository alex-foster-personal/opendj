"""Install, run and query the Carabiner sidecar that bridges Open DJ to Ableton Link.

Mini-PRD (B2B Link ops, Sat 10 Oct 2026; contract: docs/b2b-link/CONTRACT.md -> Ops):

- `up`: download the PINNED Carabiner release (Deep Symmetry, GPLv2) for macOS, verify the
  DMG sha256 AND the extracted binary sha256 AND the Developer ID team, install it under
  ~/.local/opendj/carabiner/<version>/, and start it as a plain user-run background process
  (own session, survives the shell, NOT a launchd job) on 127.0.0.1:<port> with
  AF_SERVICE_ID=com.af.opendj.carabiner.
    [if] the downloaded DMG hash differs from PINNED_DMG_SHA256 [then] exit 1, nothing installed
    [if] the port already answers `version "1.2.0"` [then] report already running, exit 0
    [if] the port is held by something that is not Carabiner [then] exit 1 naming the port
- `status`: speak Carabiner's line protocol and print version, port open, peers, bpm, beat.
    [if] nothing listens on the port [then] print `port open: no` and exit 1
    [if] the port answers but `status` will not parse [then] exit 1 with the raw line
    [if] peers is 0 [then] still exit 0 but print a WARN that no other Link app is visible
- `down`: stop the Carabiner this tool started (pidfile), refusing to kill a PID whose command
  is not our installed Carabiner.

Licensing: Carabiner is GPL-2.0. This script downloads upstream's own signed, notarized binary
from upstream's GitHub release at the user's request. Open DJ does not bundle, modify or
redistribute it, and the app talks to it only over a localhost TCP socket (ADR link-tempo).

Stdlib only so it runs before `uv sync`: `uv run --no-project python -m scripts.carabiner_sidecar`.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# The pins, download, verification and install live in the engine module (LINK-122), so this CLI
# and the packaged app verify the binary identically. It is stdlib only, like this script.
from apps.ableton_link import carabiner_provider as provider
from apps.ableton_link.carabiner_provider import CARABINER_VERSION, INSTALL_ROOT, CarabinerProvisionError

# ----------------------------------------------------------------------------- config
DEFAULT_PORT: int = 17000
SERVICE_ID: str = "com.af.opendj.carabiner"
STARTUP_DEADLINE_S: float = 5.0
SOCKET_TIMEOUT_S: float = 2.0

_FIELD = re.compile(r":(?P<key>[a-z][a-z-]*)\s+(?P<value>[^\s}]+)")


class SidecarError(RuntimeError):
    """A named, user-facing reason the sidecar step failed."""


# ----------------------------------------------------------------------------- protocol
@dataclass(frozen=True)
class CarabinerStatus:
    peers: int
    bpm: float
    start_us: int
    beat: float


def parse_edn_fields(line: str) -> dict[str, str]:
    """`kind { :a 1 :b 2 }` -> {"a": "1", "b": "2"}; the caller types the values."""
    return {m.group("key"): m.group("value") for m in _FIELD.finditer(line)}


class CarabinerLine:
    """One blocking request/response connection to a Carabiner on 127.0.0.1:<port>.

    Carabiner pushes unprompted `status` lines on any session change, so `ask` skips lines of
    other kinds until it sees the reply kind it expects.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        try:
            self._sock = socket.create_connection(("127.0.0.1", port), timeout=SOCKET_TIMEOUT_S)
        except OSError as exc:
            raise SidecarError(f"nothing answers on 127.0.0.1:{port} ({exc.strerror or exc})") from exc
        self._sock.settimeout(SOCKET_TIMEOUT_S)
        self._buffer = b""

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> CarabinerLine:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _read_line(self, deadline: float) -> str:
        while b"\n" not in self._buffer:
            if time.monotonic() > deadline:
                raise SidecarError(f"Carabiner on :{self.port} sent no complete line before the deadline")
            try:
                chunk = self._sock.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                raise SidecarError(f"Carabiner on :{self.port} closed the connection")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode("utf-8", "replace").strip()

    def ask(self, command: str, reply_kind: str, timeout_s: float = SOCKET_TIMEOUT_S) -> str:
        self._sock.sendall(command.encode() + b"\n")
        deadline = time.monotonic() + timeout_s
        while True:
            line = self._read_line(deadline)
            if line.startswith(reply_kind):
                return line
            if line.startswith(("bad-", "unsupported")):
                raise SidecarError(f"Carabiner on :{self.port} rejected {command!r}: {line!r}")

    def version(self) -> str:
        line = self.ask("version", "version")
        match = re.match(r'^version\s+"(?P<v>[^"]+)"', line)
        if match is None:
            raise SidecarError(f"unparseable version reply on :{self.port}: {line!r}")
        return match.group("v")

    def status(self) -> CarabinerStatus:
        line = self.ask("status", "status")
        return parse_status(line, self.port)

    def set_bpm(self, bpm: float) -> None:
        self._sock.sendall(f"bpm {bpm:.6f}\n".encode())

    def beat_at_time(self, when_us: int, quantum: float) -> float:
        line = self.ask(f"beat-at-time {when_us} {quantum}", "beat-at-time")
        return float(parse_edn_fields(line)["beat"])

    def force_beat_at_time(self, beat: float, when_us: int, quantum: float) -> CarabinerStatus:
        line = self.ask(f"force-beat-at-time {beat} {when_us} {quantum}", "status")
        return parse_status(line, self.port)


def parse_status(line: str, port: int) -> CarabinerStatus:
    fields = parse_edn_fields(line)
    try:
        return CarabinerStatus(
            peers=int(fields["peers"]),
            bpm=float(fields["bpm"]),
            start_us=int(fields["start"]),
            beat=float(fields["beat"]),
        )
    except (KeyError, ValueError) as exc:
        raise SidecarError(f"unparseable status line on :{port}: {line!r}") from exc


def port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


# ----------------------------------------------------------------------------- install
def installed_binary() -> Path:
    return provider.installed_binary(INSTALL_ROOT)


def install_pinned_carabiner() -> Path:
    """Download, verify and install the pinned release (engine module); idempotent when verified."""
    try:
        return provider.install_pinned_carabiner(INSTALL_ROOT, log=print)
    except CarabinerProvisionError as exc:
        raise SidecarError(str(exc)) from exc


# ----------------------------------------------------------------------------- process
def pidfile(port: int) -> Path:
    return INSTALL_ROOT / f"carabiner-{port}.pid"


def logfile(port: int) -> Path:
    return INSTALL_ROOT / f"carabiner-{port}.log"


def spawn_carabiner(binary: Path, port: int, service_id: str = SERVICE_ID) -> int:
    """Start Carabiner in its own session so it outlives this shell; returns the PID."""
    log = logfile(port).open("ab")
    process = subprocess.Popen(
        [str(binary), "--port", str(port), "--daemon"],
        env={**os.environ, "AF_SERVICE_ID": service_id},
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process.pid


def wait_for_version(port: int, deadline_s: float = STARTUP_DEADLINE_S) -> str:
    deadline = time.monotonic() + deadline_s
    last_error = "never tried"
    while time.monotonic() < deadline:
        try:
            with CarabinerLine(port) as line:
                return line.version()
        except SidecarError as exc:
            last_error = str(exc)
            time.sleep(0.2)
    raise SidecarError(f"Carabiner did not answer `version` on :{port} within {deadline_s}s ({last_error})")


def link_up(port: int) -> None:
    if port_is_open(port):
        try:
            version = wait_for_version(port, 2.0)
        except SidecarError as exc:
            raise SidecarError(f"port {port} is held by something that is not Carabiner: {exc}") from exc
        print(f"[OK] Carabiner {version} already running on 127.0.0.1:{port}")
        return
    binary = install_pinned_carabiner()
    pid = spawn_carabiner(binary, port)
    pidfile(port).write_text(f"{pid}\n")
    version = wait_for_version(port)
    if version != CARABINER_VERSION:
        raise SidecarError(f"port {port} answered version {version}, expected {CARABINER_VERSION}")
    print(f"[OK] Carabiner {version} running on 127.0.0.1:{port} pid {pid} ({SERVICE_ID}), log {logfile(port)}")
    print("[OK] user-run background process (not launchd): it stops at logout/reboot; re-run `just link-up`")


def link_status(port: int) -> int:
    if not port_is_open(port):
        print(f"[ERROR] port open: no -- nothing listens on 127.0.0.1:{port}. Run `just link-up`.")
        return 1
    with CarabinerLine(port) as line:
        version = line.version()
        status = line.status()
    print(f"[OK] carabiner version: {version}")
    print(f"[OK] port open: yes (127.0.0.1:{port})")
    print(f"[OK] peers: {status.peers}")
    print(f"[OK] bpm: {status.bpm:.3f}")
    print(f"[OK] beat: {status.beat:.3f}")
    if status.peers == 0:
        print(
            "[WARN] peers 0: no other Link app is visible. Check same network, no VPN, no client isolation, "
            "and macOS Local Network permission for Carabiner/Terminal (docs/b2b-link/FRIEND-SETUP.md)."
        )
    return 0


def link_down(port: int) -> None:
    path = pidfile(port)
    if not path.exists():
        raise SidecarError(f"no pidfile at {path}; this tool did not start a Carabiner on :{port}")
    pid = int(path.read_text().strip())
    command = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    if str(installed_binary()) not in command:
        path.unlink()
        raise SidecarError(f"pid {pid} is not our Carabiner (command {command!r}); removed stale pidfile")
    os.kill(pid, 15)
    path.unlink()
    print(f"[OK] stopped Carabiner pid {pid} on :{port}")


# ----------------------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=["up", "status", "down", "install"])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        if args.action == "up":
            link_up(args.port)
        elif args.action == "status":
            return link_status(args.port)
        elif args.action == "down":
            link_down(args.port)
        elif args.action == "install":
            install_pinned_carabiner()
    except SidecarError as exc:
        print(f"[ERROR] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
