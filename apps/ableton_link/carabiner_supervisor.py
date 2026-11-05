"""The engine starts and supervises Carabiner itself when LINK is pressed and nothing answers.

LINK-122..124 (ADR-0176-link-provider-and-local-network-permission, option (b2)). The packaged app
has no terminal, so before this only developer tools (``just link-up``, the launchd agent) ever
started Carabiner. Now the LINK join path (``routes/link_tempo.py``) dials first, exactly as before,
and only when nothing answers asks :class:`CarabinerSupervisor` to provide one:

- something already answers on the port (dev launchd agent, ``just link-up``): use it, spawn
  nothing, source ``external``;
- the port is open but the first dial failed and a TCP connect now succeeds: also ``external``
  (it came up between the two dials);
- not macOS: today's behavior, the client's own "Carabiner is not answering" error plus one
  sentence saying the engine only starts Carabiner on macOS (source ``unmanaged``);
- inside the App Store sandbox: refused by name (``sandboxed``), never downloaded or run;
- otherwise: install the pinned release on first use (``carabiner_provider``), spawn it as THIS
  engine's child (``--port <port> --daemon``, ``AF_SERVICE_ID=com.af.opendj.carabiner``, same
  process group, so the shell's group kill reaches it), wait for a ``status`` answer, then keep
  it alive: a child that exits is respawned on a capped back-off and the joined client's LINK-121
  re-dial reconnects on its own. Engine shutdown terminates it.

Every failure is a ``CarabinerProvisionError`` with a stable code the route returns as
``[carabiner_<code>] <message>``; ``status()`` names the source (``external`` | ``supervised`` with
the pid | ``installing`` | ``failed:<code>`` ...) for ``GET /api/v1/link``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from apps.ableton_link.carabiner import parse_status_line
from apps.ableton_link.carabiner_provider import CarabinerProvisionError
from apps.shared.sandbox import is_sandboxed, store_build_refusal_message

# ----------------------------------------------------------------------------- config
SERVICE_ID: str = "com.af.opendj.carabiner"
#: A freshly spawned Carabiner must answer ``status`` within this, or it is ``not_answering``.
ANSWER_DEADLINE_S: float = 10.0
PROBE_TIMEOUT_S: float = 0.5
#: Seconds before respawn attempt 1, 2, 3 ...; the last value repeats (capped, never a hot loop).
RESPAWN_BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0)
#: A child that stayed up this long before exiting resets the back-off to its first step.
STABLE_AFTER_S: float = 30.0
TERMINATE_GRACE_S: float = 3.0
LOG_TAIL_BYTES: int = 600

Decision = Literal["reuse", "spawn", "refuse_platform", "refuse_sandbox", "refuse_port_held"]
Source = Literal["idle", "external", "installing", "starting", "supervised", "respawning", "failed", "unmanaged"]

_LOG = logging.getLogger(__name__)


# ----------------------------------------------------------------------------- pure decisions
def decide_provision(*, port_open: bool, answers_status: bool, platform: str, sandboxed: bool) -> Decision:
    """What to do after the client's dial found nothing: reuse, spawn, or refuse (and why)."""
    if port_open and answers_status:
        return "reuse"
    if port_open:
        return "refuse_port_held"
    if platform != "darwin":
        return "refuse_platform"
    if sandboxed:
        return "refuse_sandbox"
    return "spawn"


def respawn_delay_s(attempt: int, backoff: tuple[float, ...] = RESPAWN_BACKOFF_S) -> float:
    """Seconds before respawn ``attempt`` (1-based), capped at the last step."""
    return backoff[min(max(attempt, 1), len(backoff)) - 1]


def unmanaged_note(platform: str) -> str:
    return (f"Open DJ starts Carabiner by itself only on macOS; on {platform} start Carabiner yourself "
            "(docs/b2b-link/FRIEND-SETUP.md).")


# ----------------------------------------------------------------------------- probes
def port_is_open(port: int, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout_s):
            return True
    except OSError:
        return False


def answers_status(port: int, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """True when 127.0.0.1:``port`` answers ``status`` with a parseable Carabiner status line."""
    deadline = time.monotonic() + timeout_s
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout_s) as sock:
            sock.sendall(b"status\n")
            buffer = b""
            while time.monotonic() < deadline:
                sock.settimeout(max(deadline - time.monotonic(), 0.01))
                chunk = sock.recv(4096)
                if not chunk:
                    return False
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if parse_status_line(raw.decode("utf-8", "replace")) is not None:
                        return True
    except (OSError, ValueError):
        return False
    return False


# ----------------------------------------------------------------------------- supervisor
@dataclass(frozen=True)
class SupervisorStatus:
    source: Source
    pid: int | None
    restarts: int
    code: str | None
    detail: str | None

    @property
    def label(self) -> str:
        """``external`` | ``supervised`` | ``installing`` | ``failed:<code>`` ... for the wire."""
        return f"failed:{self.code}" if self.source == "failed" else self.source


class CarabinerSupervisor:
    """Owns at most one Carabiner child for one port. Thread-safe; ``stop`` is final."""

    def __init__(
        self,
        port: int,
        *,
        install: Callable[[], Path],
        platform: str | None = None,
        sandboxed: Callable[[], bool] = is_sandboxed,
        answer_deadline_s: float = ANSWER_DEADLINE_S,
        backoff: tuple[float, ...] = RESPAWN_BACKOFF_S,
        stable_after_s: float = STABLE_AFTER_S,
    ) -> None:
        self.port = port
        self._install = install
        self._platform = sys.platform if platform is None else platform
        self._sandboxed = sandboxed
        self._answer_deadline_s = answer_deadline_s
        self._backoff = backoff
        self._stable_after_s = stable_after_s
        self._lock = threading.Lock()
        self._provision_lock = threading.Lock()
        self._stop = threading.Event()
        self._proc: subprocess.Popen[bytes] | None = None
        self._binary: Path | None = None
        self._monitor: threading.Thread | None = None
        self._source: Source = "idle"
        self._code: str | None = None
        self._detail: str | None = None
        self._restarts = 0

    # ------------------------------------------------------------- public
    def status(self) -> SupervisorStatus:
        with self._lock:
            proc = self._proc
            pid = proc.pid if proc is not None and proc.poll() is None else None
            return SupervisorStatus(self._source, pid, self._restarts, self._code, self._detail)

    def note_dialed(self) -> None:
        """The client's dial succeeded. With no live child of ours, whoever answered is external."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            if self._source != "external":
                _LOG.info("Link: Carabiner already answering on :%d (external: launchd agent or just link-up); "
                          "using it, not spawning", self.port)
            self._set("external", None, f"a Carabiner Open DJ did not start answers on 127.0.0.1:{self.port}")

    def provision(self) -> Decision:
        """Make a Carabiner answer on the port, after the client's dial found nothing.

        Returns the decision taken (``reuse``, ``spawn`` or ``refuse_platform``, which leaves
        today's behavior to the caller); raises ``CarabinerProvisionError`` for every failure."""
        with self._provision_lock:
            if self._stop.is_set():
                raise CarabinerProvisionError("spawn_failed", "the engine is shutting down")
            with self._lock:
                live = self._proc is not None and self._proc.poll() is None
            monitor = self._monitor
            if live or (monitor is not None and monitor.is_alive()):
                # Our child is up, or the supervisor is between respawns: one owner, so wait for it.
                self._wait_answering_live()
                return "spawn"
            decision = decide_provision(
                port_open=port_is_open(self.port),
                answers_status=answers_status(self.port),
                platform=self._platform,
                sandboxed=self._sandboxed(),
            )
            if decision == "reuse":
                self.note_dialed()
            elif decision == "refuse_platform":
                with self._lock:
                    self._set("unmanaged", None, unmanaged_note(self._platform))
            elif decision == "refuse_sandbox":
                self._fail(CarabinerProvisionError("sandboxed", store_build_refusal_message(
                    "Ableton Link through Carabiner",
                    because="The macOS App Sandbox does not let the app start the Carabiner helper.",
                )))
            elif decision == "refuse_port_held":
                self._fail(CarabinerProvisionError(
                    "port_held",
                    f"127.0.0.1:{self.port} is held by a program that is not Carabiner, so Open DJ cannot start "
                    f"Carabiner there. Quit that program, or set MDT_CARABINER_PORT to a free port.",
                ))
            elif decision == "spawn":
                self._spawn_first()
            else:
                raise AssertionError(f"unhandled decision {decision!r}")
            return decision

    def stop(self) -> None:
        """Engine shutdown: no respawn follows; our child is terminated (killed after a grace)."""
        with self._lock:
            self._stop.set()
            proc, self._proc = self._proc, None
        if proc is not None:
            _terminate(proc)
            _LOG.info("Link: stopped the Carabiner child pid %d on :%d", proc.pid, self.port)
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.join(timeout=TERMINATE_GRACE_S + 1.0)

    # ------------------------------------------------------------- spawn
    def _spawn_first(self) -> None:
        with self._lock:
            self._set("installing", None, "fetching and verifying the pinned Carabiner")
        try:
            binary = self._install()
        except CarabinerProvisionError as exc:
            self._fail(exc)
        with self._lock:
            self._binary = binary
            self._set("starting", None, f"starting {binary}")
        try:
            proc = self._spawn_and_wait(binary)
        except CarabinerProvisionError as exc:
            self._fail(exc)
        with self._lock:
            self._set("supervised", None, f"Open DJ started Carabiner (pid {proc.pid}) on 127.0.0.1:{self.port}")
        _LOG.info("Link: started Carabiner pid %d on :%d (%s)", proc.pid, self.port, binary)
        self._monitor = threading.Thread(target=self._supervise, args=(proc,), name="carabiner-supervisor", daemon=True)
        self._monitor.start()

    def _spawn(self, binary: Path) -> subprocess.Popen[bytes]:
        log_path = binary.parent / f"carabiner-{self.port}.engine.log"
        with self._lock:
            if self._stop.is_set():
                raise CarabinerProvisionError("spawn_failed", "the engine is shutting down")
            try:
                with log_path.open("ab") as log:
                    proc = subprocess.Popen(
                        [str(binary), "--port", str(self.port), "--daemon"],
                        env={**os.environ, "AF_SERVICE_ID": SERVICE_ID},
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
            except OSError as exc:
                raise CarabinerProvisionError("spawn_failed", f"could not start {binary}: {exc}") from exc
            self._proc = proc  # registered under the lock, so stop() always sees it
        return proc

    def _spawn_and_wait(self, binary: Path) -> subprocess.Popen[bytes]:
        proc = self._spawn(binary)
        deadline = time.monotonic() + self._answer_deadline_s
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                raise CarabinerProvisionError(
                    "spawn_failed",
                    f"Carabiner exited with code {code} right after starting. Log tail: {_log_tail(binary, self.port)!r}",
                )
            if answers_status(self.port):
                return proc
            time.sleep(0.1)
        _terminate(proc)
        raise CarabinerProvisionError(
            "not_answering",
            f"Carabiner started (pid {proc.pid}) but did not answer on 127.0.0.1:{self.port} within "
            f"{self._answer_deadline_s:g} s, so Open DJ stopped it.",
        )

    def _wait_answering_live(self) -> None:
        deadline = time.monotonic() + self._answer_deadline_s
        while time.monotonic() < deadline:
            if answers_status(self.port):
                return
            time.sleep(0.1)
        raise CarabinerProvisionError(
            "not_answering", f"Open DJ's Carabiner is running but did not answer on 127.0.0.1:{self.port} "
                             f"within {self._answer_deadline_s:g} s"
        )

    # ------------------------------------------------------------- supervise
    def _supervise(self, proc: subprocess.Popen[bytes]) -> None:
        """Respawn our child whenever it exits, until stop(); hand over if someone else took the port."""
        attempt = 0
        binary = self._binary
        assert binary is not None
        while True:
            started = time.monotonic()
            exit_reason: int | str = proc.wait()
            if self._stop.is_set():
                return
            attempt = 1 if time.monotonic() - started >= self._stable_after_s else attempt + 1
            _LOG.warning("Link: Carabiner pid %d exited with code %s; respawning (attempt %d)", proc.pid, exit_reason, attempt)
            while True:
                with self._lock:
                    self._set("respawning", None, f"Carabiner exited ({exit_reason}); respawn attempt {attempt}")
                if self._stop.wait(respawn_delay_s(attempt, self._backoff)):
                    return
                if answers_status(self.port):
                    with self._lock:
                        self._proc = None
                    self.note_dialed()
                    return
                try:
                    proc = self._spawn_and_wait(binary)
                except CarabinerProvisionError as exc:
                    _LOG.warning("Link: Carabiner respawn %d failed: %s", attempt, exc)
                    exit_reason = exc.code
                    attempt += 1
                    continue
                with self._lock:
                    self._restarts += 1
                    self._set("supervised", None, f"Open DJ restarted Carabiner (pid {proc.pid}, restart {self._restarts})")
                _LOG.warning("Link: Carabiner respawned as pid %d on :%d", proc.pid, self.port)
                break

    # ------------------------------------------------------------- state
    def _set(self, source: Source, code: str | None, detail: str | None) -> None:
        """Caller holds ``self._lock``."""
        self._source, self._code, self._detail = source, code, detail

    def _fail(self, exc: CarabinerProvisionError) -> NoReturn:
        with self._lock:
            self._set("failed", exc.code, str(exc))
        _LOG.warning("Link: Carabiner %s: %s", exc.code, exc)
        raise exc


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=TERMINATE_GRACE_S)


def _log_tail(binary: Path, port: int) -> str:
    path = binary.parent / f"carabiner-{port}.engine.log"
    with contextlib.suppress(OSError):
        data = path.read_bytes()
        return data[-LOG_TAIL_BYTES:].decode("utf-8", "replace").strip()
    return f"(no log at {path})"


__all__ = [
    "ANSWER_DEADLINE_S",
    "RESPAWN_BACKOFF_S",
    "SERVICE_ID",
    "CarabinerSupervisor",
    "Decision",
    "SupervisorStatus",
    "answers_status",
    "decide_provision",
    "port_is_open",
    "respawn_delay_s",
    "unmanaged_note",
]
