"""Supervise ``odj-audio``, the Rust audio engine (GSD plan 20-02).

The desktop shell supervises this Python engine; this Python engine
supervises ``odj-audio`` (phase 20 decision D5). The shell only bundles the
binary and sets ``ODJ_AUDIO_BIN`` to its absolute path. In a checkout with no
``ODJ_AUDIO_BIN``, the newest local cargo build under
``apps/audio-engine/target`` is used instead.

The supervisor starts ``odj-audio serve --clock wall|device --ws 127.0.0.1:0``
with a fresh random token in ``ODJ_AUDIO_WS_TOKEN``, reads the engine's first
stdout line (``hello``, which names the socket's URL), and publishes the URL
and token through ``GET /api/v1/audio-engine``. The renderer and agents then
connect to the engine's socket directly, so its 30 Hz state feed never passes
through Python. The supervisor keeps draining stdout (the engine broadcasts
state to its stdio client too) so the pipe never fills.

Lifecycle:

- Stdin stays open for the engine's life. Closing it is how ``stop()`` asks
  the engine to exit, and it is also what stops the engine when this process
  dies, because the kernel closes the pipe.
- An engine that exits on its own is restarted with backoff, with a new token.
  ``generation`` counts spawns, so a client can tell a restart happened.
  After ``crash_limit`` exits inside ``crash_window_s`` the supervisor stops
  trying and reports ``failed`` with the engine's last stderr lines.
- A hello that is not protocol v1 is ``failed`` at once: restarting cannot
  fix a binary that speaks another protocol.

``ODJ_AUDIO_ENGINE`` (``off`` by default, or ``wall`` / ``device``) decides
whether the engine starts with this process. Nothing plays through it yet:
the page keeps its Web Audio engine until plan 20-07, so the default leaves
the output device alone. ``POST /api/v1/audio-engine/start`` starts it on
demand either way.

Requirements (mini-PRD, NAE-07 and NAE-08 in ``.planning/REQUIREMENTS.md``):
  [if] ``ODJ_AUDIO_BIN`` is set but is not an executable file [then] the
    status is ``unavailable`` naming it, and no repo build is used instead ⛔️
  [if] the engine's hello is not protocol v1 [then] the status is ``failed``
    and it is not restarted ⛔️
  [if] the engine exits on its own [then] it is restarted with a new token
    and ``generation`` goes up ⛔️
  [if] ``stop()`` is called [then] the engine is asked to exit by stdin EOF
    and the status is ``stopped``, not a crash ⛔️
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import queue
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

from apps.shared.odj_audio_binary import (
    BIN_ENV,
    EXE_NAME,
    REPO_TARGET,
    Binary,
    OdjAudioUnavailable,
    find_binary,
)

log = logging.getLogger(__name__)

TOKEN_ENV: str = "ODJ_AUDIO_WS_TOKEN"
AUTOSTART_ENV: str = "ODJ_AUDIO_ENGINE"

Clock = Literal["wall", "device"]
CLOCKS: tuple[str, ...] = ("wall", "device")
AUTOSTART_VALUES: tuple[str, ...] = ("off", *CLOCKS)

PROTOCOL_VERSION: int = 1
WS_BIND: str = "127.0.0.1:0"
THREAD_NAME: str = "engine.audio-supervisor"

State = Literal[
    "off", "starting", "running", "restarting", "stopped", "failed", "unavailable"
]



class AudioEngineError(RuntimeError):
    """A start that cannot go ahead. ``code`` is the wire error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def autostart_from_environ(environ: Mapping[str, str]) -> str:
    """``ODJ_AUDIO_ENGINE`` as a fail-fast enum: off (default), wall, device."""
    raw = environ.get(AUTOSTART_ENV, "off").strip().lower() or "off"
    if raw not in AUTOSTART_VALUES:
        raise ValueError(
            f"{AUTOSTART_ENV}={raw!r} is not one of {', '.join(AUTOSTART_VALUES)}"
        )
    return raw


def resolve_binary(environ: Mapping[str, str], repo_root: Path) -> Binary:
    """Find the engine binary, or raise ``AudioEngineError('unavailable')``.

    The rule lives in :func:`apps.shared.odj_audio_binary.find_binary`, shared
    with the workers' decoder: ``ODJ_AUDIO_BIN`` wins and is never
    second-guessed, because a packaged app must never fall back to a repo
    build; without it, the newest of the release and debug cargo builds.
    """
    try:
        return find_binary(environ, repo_root)
    except OdjAudioUnavailable as exc:
        raise AudioEngineError("unavailable", str(exc)) from exc


Popen = Callable[..., "subprocess.Popen[str]"]


@dataclass
class _Pending:
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    gone: str | None = None


@dataclass
class _Status:
    state: State = "off"
    clock: str | None = None
    binary: str | None = None
    binary_source: str | None = None
    pid: int | None = None
    ws_url: str | None = None
    token: str | None = None
    protocol: int | None = None
    engine: str | None = None
    sample_rate: int | None = None
    generation: int = 0
    restarts: int = 0
    last_exit_code: int | None = None
    error: str | None = None
    stderr_tail: list[str] = field(default_factory=list)


class AudioEngineSupervisor:
    """One ``odj-audio`` process at a time, restarted when it dies."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str],
        repo_root: Path,
        popen: Popen = subprocess.Popen,
        backoff_s: Sequence[float] = (0.5, 1.0, 2.0, 4.0, 8.0),
        crash_limit: int = 5,
        crash_window_s: float = 60.0,
        hello_timeout_s: float = 10.0,
        stop_timeout_s: float = 5.0,
    ) -> None:
        self._environ = dict(environ)
        self._repo_root = repo_root
        self._popen = popen
        self._backoff_s = tuple(backoff_s)
        self._crash_limit = crash_limit
        self._crash_window_s = crash_window_s
        self._hello_timeout_s = hello_timeout_s
        self._stop_timeout_s = stop_timeout_s
        # _control serializes start() and stop(); _lock guards the status and
        # is never held across a wait.
        self._control = threading.Lock()
        self._lock = threading.Lock()
        self._status = _Status()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen[str] | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        # Commands this process sent on the engine's stdio, by id, waiting on
        # their result line. The engine answers stdio's commands on stdio only.
        self._pending: dict[str, _Pending] = {}
        self._next_id = 0

    # ----- public ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        # Lock held by the caller.
        s = self._status
        live = s.state == "running"
        return {
            "state": s.state,
            "clock": s.clock,
            "binary": s.binary,
            "binary_source": s.binary_source,
            "pid": s.pid if live else None,
            "ws_url": s.ws_url if live else None,
            "token": s.token if live else None,
            "protocol": s.protocol,
            "engine": s.engine,
            "sample_rate": s.sample_rate,
            "generation": s.generation,
            "restarts": s.restarts,
            "last_exit_code": s.last_exit_code,
            "error": s.error,
            "stderr_tail": list(self._stderr),
        }

    def autostart(self) -> None:
        """Start on the clock ``ODJ_AUDIO_ENGINE`` names, if any.

        A missing binary is reported in the status, not raised: the Python
        engine must still boot on a machine with no Rust build.
        """
        clock = autostart_from_environ(self._environ)
        if clock == "off":
            return
        try:
            self.start(clock)
        except AudioEngineError as exc:
            log.warning("audio engine did not autostart: %s", exc)

    def start(self, clock: str) -> dict[str, Any]:
        """Start the engine on ``clock``. Idempotent for the same clock."""
        with self._control:
            return self._start(clock)

    def _start(self, clock: str) -> dict[str, Any]:
        if clock not in CLOCKS:
            raise AudioEngineError(
                "invalid", f"clock must be one of {', '.join(CLOCKS)}, got {clock!r}"
            )
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            if alive and self._status.clock == clock:
                return self._snapshot()
            if alive:
                raise AudioEngineError(
                    "conflict",
                    f"the engine is on the {self._status.clock} clock; stop it first",
                )
        try:
            binary = resolve_binary(self._environ, self._repo_root)
        except AudioEngineError as exc:
            with self._lock:
                self._status = _Status(
                    state="unavailable",
                    clock=clock,
                    generation=self._status.generation,
                    error=str(exc),
                )
            raise
        self._stop.clear()
        self._stderr.clear()
        with self._lock:
            self._status = _Status(
                state="starting",
                clock=clock,
                binary=str(binary.path),
                binary_source=binary.source,
                generation=self._status.generation,
            )
            self._thread = threading.Thread(
                target=self._supervise, args=(clock,), name=THREAD_NAME, daemon=True
            )
            self._thread.start()
            return self._snapshot()

    def stop(self) -> dict[str, Any]:
        """Ask the engine to exit (stdin EOF), then escalate if it will not."""
        with self._control:
            return self._stop_engine()

    def _stop_engine(self) -> dict[str, Any]:
        self._stop.set()
        with self._lock:
            proc = self._proc
            thread = self._thread
        if proc is not None:
            _close_quietly(proc.stdin)
        if thread is not None:
            thread.join(self._stop_timeout_s + 2.0)
        with self._lock:
            if self._status.state in ("starting", "running", "restarting"):
                self._status.state = "stopped"
            return self._snapshot()

    def command(self, cmd: dict[str, Any], timeout_s: float = 60.0) -> dict[str, Any]:
        """Send one protocol v1 command on stdio and wait for its result.

        Returns the result line. Raises ``AudioEngineError`` when the engine
        is not running, refuses the command (``code`` is the engine's own:
        ``invalid``, ``decode``, ``not_implemented``...), exits first, or
        does not answer in ``timeout_s``.
        """
        with self._lock:
            proc = self._proc
            if self._status.state != "running" or proc is None or proc.stdin is None:
                raise AudioEngineError(
                    "not_running", f"the audio engine is {self._status.state}"
                )
            self._next_id += 1
            cid = f"py{self._next_id}"
            waiter = _Pending()
            self._pending[cid] = waiter
        try:
            try:
                proc.stdin.write(json.dumps({"id": cid, "cmd": cmd}) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise AudioEngineError(
                    "not_running", f"could not write to odj-audio: {exc}"
                ) from exc
            if not waiter.done.wait(timeout_s):
                raise AudioEngineError(
                    "timeout", f"odj-audio did not answer {cmd.get('type')} in {timeout_s:.0f} s"
                )
        finally:
            with self._lock:
                self._pending.pop(cid, None)
        if waiter.gone is not None:
            raise AudioEngineError("not_running", waiter.gone)
        result = waiter.result or {}
        if not result.get("ok"):
            err = result.get("error") or {}
            raise AudioEngineError(
                str(err.get("code", "unknown")), str(err.get("message", "refused"))
            )
        return result

    def _on_line(self, line: str) -> None:
        # State arrives 30 times a second; only results are worth parsing.
        if '"result"' not in line:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if not isinstance(msg, dict) or msg.get("type") != "result":
            return
        with self._lock:
            waiter = self._pending.get(str(msg.get("id")))
        if waiter is not None:
            waiter.result = msg
            waiter.done.set()

    def _fail_pending(self, why: str) -> None:
        with self._lock:
            waiting = list(self._pending.values())
        for w in waiting:
            w.gone = why
            w.done.set()

    # ----- the supervising thread ----------------------------------------
    def _set(self, **changes: Any) -> None:
        with self._lock:
            for k, v in changes.items():
                setattr(self._status, k, v)

    def _supervise(self, clock: str) -> None:
        crashes: collections.deque[float] = collections.deque()
        attempt = 0
        while not self._stop.is_set():
            try:
                binary = resolve_binary(self._environ, self._repo_root)
            except AudioEngineError as exc:
                self._set(state="unavailable", error=str(exc))
                return
            outcome = self._run_once(binary, clock)
            if self._stop.is_set():
                self._set(state="stopped")
                return
            if outcome == "fatal":
                return
            now = time.monotonic()
            crashes.append(now)
            while crashes and now - crashes[0] > self._crash_window_s:
                crashes.popleft()
            if len(crashes) >= self._crash_limit:
                self._set(
                    state="failed",
                    error=(
                        f"odj-audio exited {len(crashes)} times in "
                        f"{self._crash_window_s:.0f} s; not restarting"
                    ),
                )
                return
            delay = self._backoff_s[min(attempt, len(self._backoff_s) - 1)]
            attempt += 1
            with self._lock:
                self._status.state = "restarting"
                self._status.restarts += 1
            log.warning("odj-audio exited; restarting in %.1f s", delay)
            if self._stop.wait(delay):
                self._set(state="stopped")
                return

    def _run_once(self, binary: Binary, clock: str) -> Literal["exited", "fatal"]:
        token = secrets.token_urlsafe(32)
        env = dict(self._environ)
        env[TOKEN_ENV] = token
        argv = [str(binary.path), "serve", "--clock", clock, "--ws", WS_BIND]
        try:
            proc = self._popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self._set(state="failed", error=f"could not start {binary.path}: {exc}")
            return "fatal"
        lines: queue.Queue[str | None] = queue.Queue()
        _pump(proc.stdout, lines.put, "engine.audio-stdout")
        _pump(proc.stderr, self._on_stderr, "engine.audio-stderr")
        with self._lock:
            self._proc = proc
            self._status.generation += 1
            self._status.pid = proc.pid
            self._status.binary = str(binary.path)
            self._status.binary_source = binary.source
        # A stop() that raced the spawn closed nothing; close it here.
        if self._stop.is_set():
            _close_quietly(proc.stdin)
        try:
            hello = self._await_hello(lines)
            if isinstance(hello, str):
                # Not a v1 engine: restarting cannot fix that.
                self._set(state="failed", error=hello)
                self._terminate(proc)
                return "fatal"
            if hello is not None:
                with self._lock:
                    self._status.state = "running"
                    self._status.ws_url = hello["ws"]
                    self._status.token = token
                    self._status.protocol = hello["protocol"]
                    self._status.engine = hello.get("engine")
                    self._status.sample_rate = hello.get("sample_rate")
                    self._status.error = None
                log.info("odj-audio %s up on %s", clock, hello["ws"])
                # Drain until EOF, or until stop() asks: an engine that
                # ignores stdin EOF keeps its stdout open, and _wait is what
                # escalates to terminate. The engine sends state to stdio too.
                while not self._stop.is_set():
                    try:
                        line = lines.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if line is None:
                        break
                    self._on_line(line)
            code = self._wait(proc)
            self._set(last_exit_code=code, pid=None, ws_url=None, token=None)
            if not self._stop.is_set():
                self._set(error=f"odj-audio exited with code {code}")
            return "exited"
        finally:
            with self._lock:
                self._proc = None
            self._fail_pending("odj-audio exited before answering")

    def _await_hello(
        self, lines: queue.Queue[str | None]
    ) -> dict[str, Any] | str | None:
        """The hello, an error string for a wrong protocol, or None on EOF."""
        try:
            first = lines.get(timeout=self._hello_timeout_s)
        except queue.Empty:
            return f"odj-audio sent no hello within {self._hello_timeout_s:.0f} s"
        if first is None:
            return None
        try:
            hello = json.loads(first)
        except ValueError:
            return f"odj-audio's first line is not JSON: {first[:200]!r}"
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            return f"odj-audio's first line is not a hello: {first[:200]!r}"
        if hello.get("protocol") != PROTOCOL_VERSION:
            return (
                f"odj-audio speaks protocol {hello.get('protocol')!r}; "
                f"this engine needs {PROTOCOL_VERSION}"
            )
        if not isinstance(hello.get("ws"), str):
            return "odj-audio's hello names no socket; is it older than plan 20-02?"
        return hello

    def _on_stderr(self, line: str | None) -> None:
        if line is None:
            return
        self._stderr.append(line)
        log.warning("odj-audio: %s", line)

    def _wait(self, proc: subprocess.Popen[str]) -> int:
        if self._stop.is_set():
            try:
                return proc.wait(self._stop_timeout_s)
            except subprocess.TimeoutExpired:
                log.warning("odj-audio ignored stdin EOF; terminating")
                return self._terminate(proc)
        return proc.wait()

    def _terminate(self, proc: subprocess.Popen[str]) -> int:
        _close_quietly(proc.stdin)
        proc.terminate()
        try:
            return proc.wait(2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()


def _pump(
    stream: IO[str] | None, sink: Callable[[str | None], None], name: str
) -> None:
    """Forward a pipe line by line, then ``None`` at EOF, on its own thread."""

    def run() -> None:
        if stream is not None:
            try:
                for raw in stream:
                    sink(raw.rstrip("\r\n"))
            except (OSError, ValueError):
                pass
        sink(None)

    threading.Thread(target=run, name=name, daemon=True).start()


def _close_quietly(stream: IO[str] | None) -> None:
    if stream is None:
        return
    with contextlib.suppress(OSError):
        stream.close()


__all__ = [
    "AUTOSTART_ENV",
    "BIN_ENV",
    "EXE_NAME",
    "REPO_TARGET",
    "TOKEN_ENV",
    "AudioEngineError",
    "AudioEngineSupervisor",
    "Binary",
    "autostart_from_environ",
    "resolve_binary",
]
