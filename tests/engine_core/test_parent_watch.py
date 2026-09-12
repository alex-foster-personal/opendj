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
    time.sleep(600)
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


def _popen_child(script: str, *args: str, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
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
    child = _popen_child(_CHILD_NO_ENV, str(tmp_path))
    time.sleep(0.8)
    assert child.poll() is None, "child without OPENDJ_PARENT_PID should stay alive"
    child.kill()
    child.wait(timeout=5)


@pytest.mark.requirement("INSTALL-14")
def test_parent_file_rebinds_the_watched_pid(tmp_path: Path) -> None:
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
