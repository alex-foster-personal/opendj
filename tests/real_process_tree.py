"""Real process trees with Open DJ-style identities, for process-family tests.

Each node is a real Python process. A ``title`` node retitles itself with
``setproctitle`` (the same call ``apps.shared.process_identity`` makes in
production). An ``argv`` node keeps the exact command line it was launched
with, ``python -m apps.webui.server --reload`` say, which is what a Windows
process list shows for an Open DJ server because setproctitle is invisible
there; a ``sitecustomize`` hook holds it before the module would run, so the
server itself never starts. Every node starts its children, reports every pid
in its subtree on one stdout line, then sleeps. A ``detach`` node double-forks
first, so it is re-parented OUTSIDE the spawning test's process tree, the way
another worktree's engine or a vite dev server sits on a real machine. Tests
read these processes back through the production psutil path; nothing here
builds a process table by hand.

Platform-specific: titles set by ``setproctitle`` are not visible in a Windows
process's command line, and ``detach`` needs POSIX ``fork``; tests skip with
those reasons. ``argv`` nodes run everywhere.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from functools import cache
from pathlib import Path
from typing import Any

import psutil
import pytest

_SPEC_ENV = "OPENDJ_TEST_NODE_SPEC"
# An ``env`` value meaning "the pid of the process that starts this node", so a
# node can launch its child the way the desktop shells launch the engine.
PARENT_PID = "<parent pid>"
_ORIG_PYTHONPATH_ENV = "OPENDJ_TEST_NODE_ORIG_PYTHONPATH"

# Runs with globals SPEC_JSON and HOOK_DIR.
_NODE = r"""
import json, os, subprocess, sys, time
PARENT_PID = %(parent_pid)r
spec = json.loads(SPEC_JSON)
if spec.get("detach"):
    if os.fork():
        os._exit(0)
    os.setsid()
if spec.get("title") is not None:
    import setproctitle
    setproctitle.setproctitle(spec["title"])
pids = {spec["key"]: os.getpid()}
for child in spec.get("children", []):
    env = dict(os.environ)
    for name, value in child.get("env", {}).items():
        env[name] = str(os.getpid()) if value == PARENT_PID else value
    if child.get("argv") is not None:
        env[%(spec_env)r] = json.dumps(child)
        env[%(orig_env)r] = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(p for p in (HOOK_DIR, env.get("PYTHONPATH", "")) if p)
        argv = [sys.executable, *child["argv"]]
    else:
        argv = [sys.executable, "-c", %(boot)r, json.dumps(child), HOOK_DIR]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, env=env)
    pids.update(json.loads(proc.stdout.readline()))
print(json.dumps(pids), flush=True)
time.sleep(float(spec.get("ttl", 180)))
os._exit(0)
"""

_BOOT = (
    "import sys; from pathlib import Path; "
    "exec(Path(sys.argv[2], 'node.py').read_text(), "
    "{'SPEC_JSON': sys.argv[1], 'HOOK_DIR': sys.argv[2]})"
)

# Imported by an ``argv`` node's interpreter at startup (its PYTHONPATH holds
# this directory); it runs the node and exits before ``-m`` would run anything.
_SITECUSTOMIZE = r"""
import os
from pathlib import Path
_spec = os.environ.pop(%(spec_env)r, None)
if _spec is not None:
    _orig = os.environ.pop(%(orig_env)r, "")
    if _orig:
        os.environ["PYTHONPATH"] = _orig
    else:
        os.environ.pop("PYTHONPATH", None)
    _dir = str(Path(__file__).parent)
    exec(Path(_dir, "node.py").read_text(), {"SPEC_JSON": _spec, "HOOK_DIR": _dir})
"""

needs_visible_titles = pytest.mark.skipif(
    sys.platform == "win32",
    reason="setproctitle titles are not visible in a Windows process command line",
)
needs_fork = pytest.mark.skipif(
    not hasattr(os, "fork"),
    reason="re-parenting a process out of the test's tree needs POSIX fork",
)


@cache
def _hook_dir() -> str:
    directory = Path(tempfile.mkdtemp(prefix="real-process-tree-"))
    names = {
        "spec_env": _SPEC_ENV,
        "orig_env": _ORIG_PYTHONPATH_ENV,
        "boot": _BOOT,
        "parent_pid": PARENT_PID,
    }
    (directory / "node.py").write_text(_NODE % names, encoding="utf-8")
    (directory / "sitecustomize.py").write_text(_SITECUSTOMIZE % names, encoding="utf-8")
    return str(directory)


def node(key: str, title: str, *children: dict[str, Any], detach: bool = False) -> dict[str, Any]:
    """A process that shows ``title`` as its OS command line (setproctitle)."""

    return {"key": key, "title": title, "children": list(children), "detach": detach}


def argv_node(
    key: str, argv: list[str], *children: dict[str, Any], env: dict[str, str] | None = None
) -> dict[str, Any]:
    """A process launched as ``python <argv...>`` that keeps that command line.

    ``env`` is added to what its parent passes it; a value of ``PARENT_PID``
    becomes the starting process's own pid. (Honored for children, which is
    every node a parent starts; a root node gets the test's environment.)
    """

    return {"key": key, "title": None, "argv": argv, "children": list(children), "env": env or {}}


def _expected_command(spec: dict[str, Any]) -> str:
    if spec.get("argv") is not None:
        return " ".join([sys.executable, *spec["argv"]])
    return spec["title"]


def _wait_for_command(pid: int, expected: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with contextlib.suppress(psutil.Error):
            if " ".join(psutil.Process(pid).cmdline()).startswith(expected):
                return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} never showed command {expected!r}")


def _all(spec: dict[str, Any]) -> list[dict[str, Any]]:
    out = [spec]
    for child in spec["children"]:
        out.extend(_all(child))
    return out


def spawn(spec: dict[str, Any]) -> dict[str, int]:
    """Start the tree; return ``{key: pid}`` once every process shows its command."""

    hook = _hook_dir()
    env = dict(os.environ)
    if spec.get("argv") is not None:
        env[_SPEC_ENV] = json.dumps(spec)
        env[_ORIG_PYTHONPATH_ENV] = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(p for p in (hook, env.get("PYTHONPATH", "")) if p)
        argv = [sys.executable, *spec["argv"]]
    else:
        argv = [sys.executable, "-c", _BOOT, json.dumps(spec), hook]
    launcher = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, env=env)
    assert launcher.stdout is not None
    # One line, not EOF: a detached survivor keeps the pipe open.
    line = launcher.stdout.readline()
    launcher.stdout.close()
    if spec.get("detach"):
        assert launcher.wait(timeout=20) == 0
    pids: dict[str, int] = json.loads(line)
    for each in _all(spec):
        _wait_for_command(pids[each["key"]], _expected_command(each))
    return pids


def _running(proc: psutil.Process) -> bool:
    # A zombie has exited; it is only waiting to be reaped and has no command line.
    with contextlib.suppress(psutil.NoSuchProcess):
        return proc.status() != psutil.STATUS_ZOMBIE
    return False


def kill(pids: dict[str, int]) -> None:
    procs: list[psutil.Process] = []
    for pid in pids.values():
        with contextlib.suppress(psutil.Error):
            procs.append(psutil.Process(pid))
    for proc in procs:
        with contextlib.suppress(psutil.NoSuchProcess):  # already gone is the goal
            proc.kill()
    # Poll status rather than psutil.wait_procs: its pidfd_open wait raised EINVAL
    # for a live pid on the shared CI runner (shard 2, pid 3891438).
    deadline = time.monotonic() + 10
    alive = [proc for proc in procs if _running(proc)]
    while alive and time.monotonic() < deadline:
        time.sleep(0.05)
        alive = [proc for proc in alive if _running(proc)]
    # A process that outlives its kill would leak into the next test's walk.
    if alive:
        raise AssertionError(f"still running after kill: {sorted(p.pid for p in alive)}")
