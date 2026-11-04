"""Fail a pytest session that leaves live child processes running.

Snapshot ``psutil.Process().children(recursive=True)`` at session start and
compare at session finish. Survivors whose ``(pid, create_time)`` was not in
the baseline are leaks unless their cmdline matches a known helper bootstrap.

Exempt cmdline substrings (helpers that outlive the test that started them):

- ``from multiprocessing.resource_tracker import main``
- ``from multiprocessing.forkserver import main``
- execnet/xdist bootstrap: ``import sys;exec(eval(sys.stdin.readline()))``

``from multiprocessing.spawn import spawn_main`` is not exempt. Zombies are not
live. Missing psutil prints UNAVAILABLE and skips the check.

-Cursor
"""
from __future__ import annotations

import sys
import time
from collections.abc import Callable

import pytest

LEAK_GRACE_S: float = 2.0

_EXEMPT_SUBSTRINGS: tuple[str, ...] = (
    "from multiprocessing.resource_tracker import main",
    "from multiprocessing.forkserver import main",
    "import sys;exec(eval(sys.stdin.readline()))",
)

_BASELINE_KEY = pytest.StashKey[dict[tuple[int, float], tuple[str, ...]] | None]()
_UNAVAILABLE_KEY = pytest.StashKey[bool]()


# ---------------------------------------------------------------- helpers
def _line_writer(config: pytest.Config) -> Callable[[str], None]:
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        return reporter.write_line  # type: ignore[no-any-return]
    return lambda line: print(line, file=sys.stderr)


def _exempt_cmdline(cmdline: tuple[str, ...]) -> bool:
    joined = " ".join(cmdline)
    return any(fragment in joined for fragment in _EXEMPT_SUBSTRINGS)


def _describe_leak(identity: tuple[int, float], cmdline: tuple[str, ...]) -> str:
    pid, _create_time = identity
    if cmdline:
        return f"pid={pid} cmdline={list(cmdline)}"
    return f"pid={pid} cmdline=<unreadable>"


def _snapshot_live_children() -> dict[tuple[int, float], tuple[str, ...]]:
    import psutil

    result: dict[tuple[int, float], tuple[str, ...]] = {}
    for proc in psutil.Process().children(recursive=True):
        try:
            if proc.status() == psutil.STATUS_ZOMBIE:
                continue
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            pass
        try:
            identity = (proc.pid, proc.create_time())
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        try:
            cmdline = tuple(proc.cmdline())
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            cmdline = ()
        result[identity] = cmdline
    return result


# ---------------------------------------------------------------- public
def snapshot_children() -> dict[tuple[int, float], tuple[str, ...]] | None:
    """Live descendants of this process, or ``None`` when psutil is missing."""
    try:
        import psutil  # noqa: F401
    except ImportError:
        return None
    return _snapshot_live_children()


def leaked_processes(
    baseline: dict[tuple[int, float], tuple[str, ...]],
    *,
    grace_s: float = LEAK_GRACE_S,
) -> list[str]:
    """Describe live descendants not in ``baseline`` after a bounded grace poll."""
    try:
        import psutil  # noqa: F401
    except ImportError:
        return []

    deadline = time.monotonic() + grace_s
    last_leaks: list[str] = []
    while True:
        current = _snapshot_live_children()
        last_leaks = [
            _describe_leak(identity, cmdline)
            for identity, cmdline in current.items()
            if identity not in baseline and not _exempt_cmdline(cmdline)
        ]
        if not last_leaks or time.monotonic() >= deadline:
            break
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    return last_leaks


# ---------------------------------------------------------------- hooks
def pytest_sessionstart(session: pytest.Session) -> None:
    baseline = snapshot_children()
    session.config.stash[_BASELINE_KEY] = baseline
    if baseline is None:
        session.config.stash[_UNAVAILABLE_KEY] = True
        _line_writer(session.config)(
            "UNAVAILABLE: psutil is not installed; child-process leak check skipped"
        )
    else:
        session.config.stash[_UNAVAILABLE_KEY] = False


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if session.config.stash.get(_UNAVAILABLE_KEY, False):
        return
    baseline = session.config.stash.get(_BASELINE_KEY, None)
    if baseline is None:
        return
    if session.config.pluginmanager.get_plugin("dsession") is not None and not hasattr(
        session.config, "workerinput"
    ):
        return
    leaks = leaked_processes(baseline)
    if not leaks:
        return
    write = _line_writer(session.config)
    write(f"[process-leaks] FAIL: {len(leaks)} leaked child process(es):")
    for leak in leaks:
        write(f"  {leak}")
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
