from __future__ import annotations

import subprocess
import sys
import time

import pytest

from apps.shared.process_identity import _os_title, process_command, process_title

_SUBPROCESS_TIMEOUT_SECONDS = 10.0

_ENGINE_LAUNCH_SOURCE = """
import time
from apps.shared.process_identity import set_process_identity
set_process_identity("Engine", 8585, invocation_marker="apps.engine_core serve")
time.sleep(10)
"""


def test_process_title_namespaces_role_and_instance() -> None:
    assert process_title("Engine", 8585) == "opendj-engine --name opendj-engine --port 8585"
    assert (
        process_title("  Worker   · setup.import-rekordbox  ")
        == "opendj-worker-setup.import-rekordbox --name opendj-worker-setup.import-rekordbox"
    )


def test_process_title_refuses_an_empty_role() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        process_title("  \t  ")


def test_os_title_preserves_an_exact_spawn_argv() -> None:
    argv = ("/opt/Open DJ/python", "-m", "apps.engine_core.setup.worker", "--root", "/tmp/A B")

    title = _os_title("opendj-worker --name opendj-worker", None, argv)

    expected = "['/opt/Open DJ/python' -m apps.engine_core.setup.worker --root '/tmp/A B']"
    assert title.endswith(expected)


def test_process_command_is_the_exact_worker_reap_identity() -> None:
    argv = (sys.executable, "-m", "apps.engine_core.setup.worker", "--data-dir", "/tmp/dj")
    assert process_command("worker", invocation_argv=argv) == (
        f"opendj-worker --name opendj-worker [{sys.executable} -m "
        "apps.engine_core.setup.worker --data-dir /tmp/dj]"
    )


def test_set_process_identity_preserves_probe_markers_in_the_os_title() -> None:
    """The OS title must still satisfy substring probes outside this process.

    setproctitle REPLACES the OS-visible command line wholesale (see
    ``_os_title``'s docstring), confirmed here through a REAL disposable
    subprocess rather than a monkeypatched ``sys.argv``/``setproctitle``
    (AGENTS.md forbids fabricating application state in tests, and a
    monkeypatched ``sys.argv`` previously modeled an impossible shape: real
    ``python -m apps.engine_core serve`` launches never leave ``-m
    apps.engine_core`` in ``sys.argv`` at all -- CPython's ``-m`` machinery
    rewrites ``sys.argv[0]`` to the resolved ``__main__.py`` path instead).
    The launched child calls ``set_process_identity`` exactly as
    ``apps/engine_core/__main__.py`` does, then this test reads the child's
    OS-visible command line back through the same ``ps`` column
    ``scripts/diagnostics/probe_process_family.py`` greps in production.
    """
    proc = subprocess.Popen([sys.executable, "-c", _ENGINE_LAUNCH_SOURCE])
    try:
        command = ""
        deadline = time.monotonic() + _SUBPROCESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["pgrep", "-f", "opendj-engine"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            found_pids = {int(pid) for pid in result.stdout.split() if pid.isdigit()}
            if proc.pid in found_pids:
                command = subprocess.run(
                    ["ps", "-p", str(proc.pid), "-ww", "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                ).stdout.strip()
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"process {proc.pid} never reported a renamed title: {command!r}")

        assert command.startswith("opendj-engine --name opendj-engine --port 8585 [")
        assert sys.executable in command
        assert "apps.engine_core serve" in command
    finally:
        proc.kill()
        proc.wait(timeout=_SUBPROCESS_TIMEOUT_SECONDS)
