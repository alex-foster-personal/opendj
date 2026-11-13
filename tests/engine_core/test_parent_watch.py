"""Parent-death watch for shell-spawned engines (issue #2160)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.waits import wait_for_external_state

_CHILD_SLEEP = textwrap.dedent(
    """
    import os, signal, sys, time
    from pathlib import Path
    from apps.engine_core import parent_watch

    data_dir = Path(sys.argv[1])
    parent_watch.start(data_dir)
    time.sleep(600)
    """
)

_CHILD_SIGTERM = textwrap.dedent(
    """
    import os, signal, sys, time
    from pathlib import Path
    from apps.engine_core import parent_watch

    marker = Path(sys.argv[1])
    data_dir = Path(sys.argv[2])

    def on_term(signum, frame):
        marker.write_text("got-term", encoding="utf-8")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)
    parent_watch.start(data_dir)
    Path(str(marker) + ".ready").write_text("watching", encoding="utf-8")
    # Short sleeps, not one long one: on Windows a main thread inside time.sleep
    # cannot run a Python signal handler until the sleep returns. The real
    # engine's event loop wakes on the signal; this loop stands in for it.
    for _ in range(12000):
        time.sleep(0.05)
    """
)

_CHILD_NO_ENV = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path
    from apps.engine_core import parent_watch

    data_dir = Path(sys.argv[1])
    os.environ.pop("OPENDJ_PARENT_PID", None)
    parent_watch.start(data_dir)
    time.sleep(600)
    """
)

_CHILD_REBIND = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path
    from apps.engine_core import parent_watch

    data_dir = Path(sys.argv[1])
    parent_watch.start(data_dir)
    time.sleep(600)
    """
)


def _popen_child(
    script: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.Popen[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        env=merged,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.requirement("INSTALL-14")
def test_watch_exits_the_child_when_the_parent_pid_disappears(tmp_path: Path) -> None:
    """
    [if] the watched parent pid disappears [then] parent_watch exits the child process, [else stop].
    """
    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    child = _popen_child(
        _CHILD_SLEEP,
        str(tmp_path),
        env={"OPENDJ_PARENT_PID": str(dummy.pid)},
    )
    dummy.kill()
    dummy.wait(timeout=5)
    wait_for_external_state(
        lambda: child.poll() is not None,
        what="child exits after dummy parent is killed",
        guard_s=2.0,
    )


@pytest.mark.requirement("INSTALL-14")
def test_watch_does_not_start_when_parent_env_is_unset(tmp_path: Path) -> None:
    """
    [if] OPENDJ_PARENT_PID is unset [then] the child is left running, unwatched, [else stop].
    """
    child = _popen_child(_CHILD_NO_ENV, str(tmp_path))
    time.sleep(0.8)
    assert child.poll() is None, "child without OPENDJ_PARENT_PID should stay alive"
    child.kill()
    child.wait(timeout=5)


@pytest.mark.requirement("INSTALL-14")
def test_parent_file_rebinds_the_watched_pid(tmp_path: Path) -> None:
    """
    [if] .engine.parent rebinds the watched pid [then] only its death exits the child, [else stop].
    """
    parent_a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    parent_b = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    child = _popen_child(
        _CHILD_REBIND,
        str(tmp_path),
        env={"OPENDJ_PARENT_PID": str(parent_a.pid)},
    )
    (tmp_path / ".engine.parent").write_text(f"{parent_b.pid}\n", encoding="utf-8")
    parent_a.kill()
    parent_a.wait(timeout=5)
    time.sleep(0.8)
    assert child.poll() is None, "child should still watch parent B via .engine.parent"
    parent_b.kill()
    parent_b.wait(timeout=5)
    wait_for_external_state(
        lambda: child.poll() is not None,
        what="child exits after parent B is killed",
        guard_s=2.0,
    )


@pytest.mark.requirement("INSTALL-14")
def test_watch_signals_sigterm_not_hard_exit(tmp_path: Path) -> None:
    """
    [if] the watched parent dies [then] parent_watch sends SIGTERM, not a hard kill, [else stop].
    """
    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    marker = tmp_path / "got-term"
    child = _popen_child(
        _CHILD_SIGTERM,
        str(marker),
        str(tmp_path),
        env={"OPENDJ_PARENT_PID": str(dummy.pid)},
    )
    dummy.kill()
    dummy.wait(timeout=5)
    wait_for_external_state(
        lambda: marker.is_file(),
        what="SIGTERM handler wrote marker",
        guard_s=2.0,
    )
    child.wait(timeout=5)


_PROBE_LIVENESS = textwrap.dedent(
    """
    import subprocess, sys
    from apps.shared import process_liveness

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        print("ALIVE", process_liveness.pid_is_alive(sleeper.pid), flush=True)
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)
    sleeper = subprocess.Popen([sys.executable, "-c", "pass"])
    sleeper.wait(timeout=10)
    print("GONE", process_liveness.pid_is_alive(sleeper.pid), flush=True)
    """
)


@pytest.mark.requirement("INSTALL-14")
def test_liveness_probe_does_not_signal_a_live_process_and_reports_both_states() -> None:
    """
    [if] the parent liveness probe runs against a live sibling [then] the prober survives and reports ALIVE True then GONE False, [else stop].

    On Windows ``os.kill(pid, 0)`` is CTRL_C_EVENT, so probing a live pid interrupts the console group.
    """
    result = subprocess.run(
        [sys.executable, "-c", _PROBE_LIVENESS],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"prober died: rc={result.returncode} stderr={result.stderr[-400:]}"
    assert result.stdout.split() == ["ALIVE", "True", "GONE", "False"], result.stdout


@pytest.mark.requirement("INSTALL-14")
def test_a_stop_file_asks_a_watched_engine_to_shut_down_through_its_sigterm_handler(tmp_path: Path) -> None:
    """
    [if] the shell writes .engine.stop while its pid is alive [then] the engine's SIGTERM handler runs and the request is consumed, [else stop].
    """
    marker = tmp_path / "got-term"
    child = _popen_child(
        _CHILD_SIGTERM,
        str(marker),
        str(tmp_path),
        env={"OPENDJ_PARENT_PID": str(os.getpid())},
    )
    # The engine discards a request older than its start, so write only once it is watching.
    wait_for_external_state(
        lambda: Path(str(marker) + ".ready").is_file(),
        what="child started its parent watch",
        guard_s=15.0,
    )
    (tmp_path / ".engine.stop").write_text("", encoding="utf-8")
    wait_for_external_state(
        lambda: marker.is_file(),
        what="SIGTERM handler wrote marker after .engine.stop",
        guard_s=5.0,
    )
    child.wait(timeout=5)
    assert not (tmp_path / ".engine.stop").exists(), "the stop request must be consumed, not left for the next engine"


@pytest.mark.requirement("INSTALL-14")
def test_a_stale_stop_file_does_not_stop_the_next_engine(tmp_path: Path) -> None:
    """
    [if] a .engine.stop left by an earlier run exists at start [then] it is discarded and the engine keeps running, [else stop].
    """
    (tmp_path / ".engine.stop").write_text("", encoding="utf-8")
    child = _popen_child(
        _CHILD_SLEEP,
        str(tmp_path),
        env={"OPENDJ_PARENT_PID": str(os.getpid())},
    )
    time.sleep(2.0)
    assert child.poll() is None, "a stale stop request must not kill a fresh engine"
    assert not (tmp_path / ".engine.stop").exists(), "the stale request is discarded at start"
    child.kill()
    child.wait(timeout=5)


@pytest.mark.requirement("INSTALL-14")
def test_a_live_parent_and_no_stop_file_leave_the_engine_running(tmp_path: Path) -> None:
    """
    [if] the parent is alive and no stop file exists [then] the engine keeps running, [else stop].
    """
    child = _popen_child(
        _CHILD_SLEEP,
        str(tmp_path),
        env={"OPENDJ_PARENT_PID": str(os.getpid())},
    )
    time.sleep(1.5)
    assert child.poll() is None, "the control: a live parent must not stop the engine"
    child.kill()
    child.wait(timeout=5)


@pytest.mark.requirement("INSTALL-42")
def test_watch_exits_the_child_when_its_stderr_reader_died_with_the_parent(tmp_path: Path) -> None:
    """
    [if] the parent and its stderr reader die [then] parent_watch still exits the child, [else stop].

    The warning cannot be written to the dead pipe and must not stop the SIGTERM.
    """
    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    child = _popen_child(
        _CHILD_SLEEP,
        str(tmp_path),
        env={"OPENDJ_PARENT_PID": str(dummy.pid)},
    )
    assert child.stderr is not None
    child.stderr.close()  # the only reader: the next write to stderr gets EPIPE
    dummy.kill()
    dummy.wait(timeout=5)
    wait_for_external_state(
        lambda: child.poll() is not None,
        what="child exits after its parent and stderr reader are gone",
        guard_s=2.0,
    )


class _StopLoop(Exception):
    """Raised by the fake sleep to end an in-process watch loop after its scripted ticks."""


def _run_watch_ticks(
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
    env_pid: int,
    alive: set[int],
    between_ticks: list[object] | None = None,
) -> tuple[list[int], int]:
    """Drive parent_watch._watch_loop in-process; return (pids probed per tick, self-SIGTERM count)."""
    from apps.engine_core import parent_watch

    probed: list[int] = []
    terms: list[int] = []
    steps = list(between_ticks or [])

    def fake_alive(pid: int) -> bool:
        """Record the probed pid and answer from the scripted live set."""
        probed.append(pid)
        return pid in alive

    def fake_sleep(_seconds: float) -> None:
        """Run the next scripted step between ticks, or stop the loop when none are left."""
        if not steps:
            raise _StopLoop
        step = steps.pop(0)
        if callable(step):
            step()

    monkeypatch.setattr(parent_watch, "pid_is_alive", fake_alive)
    monkeypatch.setattr(parent_watch, "_signal_self_term", lambda: terms.append(1))
    monkeypatch.setattr(parent_watch.time, "sleep", fake_sleep)
    try:
        parent_watch._watch_loop(data_dir, env_pid)
    except _StopLoop:
        pass
    return probed, len(terms)


_ENV_PID = 4_000_001
_MARKER_PID = 4_000_002


@pytest.mark.requirement("INSTALL-14")
def test_a_marker_with_a_valid_live_pid_wins_over_a_dead_env_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    [if] the parent marker contains a valid live pid [then] the watch probes that pid and the engine keeps running though the env pid is dead, [else stop].
    """
    (tmp_path / ".engine.parent").write_text(f"{_MARKER_PID}\n", encoding="utf-8")
    probed, terms = _run_watch_ticks(monkeypatch, tmp_path, _ENV_PID, alive={_MARKER_PID}, between_ticks=[None, None])
    assert probed == [_MARKER_PID] * 3
    assert terms == 0


@pytest.mark.requirement("INSTALL-13437")
@pytest.mark.parametrize(
    "raw",
    [b"not-a-pid\n", b"", b"   \n", b"0\n", b"-7\n", b"12abc\n", b"\xff\xfe\x00garbage"],
    ids=["text", "empty", "blank", "zero", "negative", "trailing-junk", "non-utf8"],
)
def test_a_malformed_marker_falls_back_to_the_env_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    """
    [if] the parent marker contains invalid text [then] the watch ignores it, probes the env pid, and still stops the engine when that pid is gone, [else stop].
    """
    (tmp_path / ".engine.parent").write_bytes(raw)
    probed, terms = _run_watch_ticks(monkeypatch, tmp_path, _ENV_PID, alive={_ENV_PID}, between_ticks=[None])
    assert probed == [_ENV_PID, _ENV_PID], "a malformed marker must not replace the env pid"
    assert terms == 0, "a live env pid keeps the engine running"

    probed, terms = _run_watch_ticks(monkeypatch, tmp_path, _ENV_PID, alive=set())
    assert probed == [_ENV_PID]
    assert terms == 1, "a malformed marker must not stop the watch from firing when the env pid is gone"


@pytest.mark.requirement("INSTALL-14")
def test_a_marker_that_disappears_after_launch_falls_back_to_the_env_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    [if] the parent marker disappears after engine launch [then] the next tick watches the env pid again and stops the engine once it is gone, [else stop].
    """
    marker = tmp_path / ".engine.parent"
    marker.write_text(f"{_MARKER_PID}\n", encoding="utf-8")
    probed, terms = _run_watch_ticks(
        monkeypatch, tmp_path, _ENV_PID, alive={_MARKER_PID}, between_ticks=[marker.unlink]
    )
    assert probed == [_MARKER_PID, _ENV_PID], "after the marker is removed the env pid is watched again"
    assert terms == 1, "the env pid is dead, so the engine is stopped"

    marker.write_text(f"{_MARKER_PID}\n", encoding="utf-8")
    probed, terms = _run_watch_ticks(
        monkeypatch, tmp_path, _ENV_PID, alive={_MARKER_PID, _ENV_PID}, between_ticks=[marker.unlink, None]
    )
    assert probed == [_MARKER_PID, _ENV_PID, _ENV_PID]
    assert terms == 0, "a removed marker with a live env pid leaves the engine running"
