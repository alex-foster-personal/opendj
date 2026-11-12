"""Shared helpers for the perf-lab tests (no tests here)."""

from __future__ import annotations

import contextlib
import glob
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
LAB = REPO / "scripts/perf/lab"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"perflab_{name}", LAB / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"cannot load {name}.py"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _node() -> str:
    """The absolute node every probe runs; none skips as UNAVAILABLE."""
    node = shutil.which("node")
    if not node or not Path(node).is_absolute():
        pytest.skip(f"UNAVAILABLE: no node on PATH as an absolute executable (found {node!r})")
    assert node is not None  # narrows for mypy, whose env has no pytest stubs
    return node


def _chromium() -> str:
    """A Playwright Chromium and the frontend's playwright package, or skip as UNAVAILABLE."""
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), str(Path.home() / ".cache/ms-playwright")]
    hits = [h for r in filter(None, roots) for h in sorted(glob.glob(f"{r}/chromium-*/chrome-linux*/chrome"))]
    if not hits:
        pytest.skip("UNAVAILABLE: no Playwright Chromium (Linux layout) under PLAYWRIGHT_BROWSERS_PATH or ~/.cache")
    _frontend_playwright()
    return hits[-1]


def _frontend_playwright() -> None:
    """The frontend's playwright package, which driver.mjs and abwin.mjs load,
    or skip as UNAVAILABLE (a job that runs no pnpm install has none)."""
    if not (REPO / "apps/webui/frontend/node_modules/playwright").is_dir():
        pytest.skip("UNAVAILABLE: the frontend's playwright package is not installed (pnpm install)")


def _node_json(script: str, *args: str, timeout: int = 120, env: dict[str, str] | None = None) -> dict:
    """Run an ES module script with node (under env, when given, else this
    process's environment) and parse the JSON it prints last."""
    r = subprocess.run(
        [_node(), "--input-type=module", "-e", script, *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
        env=env,
    )
    return dict(json.loads(r.stdout.strip().splitlines()[-1]))


@contextlib.contextmanager
def pulse_server() -> Iterator[dict[str, str]]:
    """A real PulseAudio server with two null sinks of one format (perflab_a,
    the default, and perflab_b), private to a fresh XDG_RUNTIME_DIR; yields
    the environment that reaches it (this process's, plus that directory).
    No pulseaudio or pactl skips as UNAVAILABLE. Killed on exit."""
    exe, pactl = shutil.which("pulseaudio"), shutil.which("pactl")
    if not exe or not pactl:
        pytest.skip("UNAVAILABLE: no pulseaudio and pactl to run a real sound server")
    assert exe is not None and pactl is not None  # narrows for mypy
    # a unix socket path is capped near 108 bytes, so a short directory, not tmp_path
    xdg = Path(tempfile.mkdtemp(prefix="pl-pulse-"))
    xdg.chmod(0o700)
    env = {k: v for k, v in os.environ.items() if k != "PULSE_SERVER"}
    env["XDG_RUNTIME_DIR"] = str(xdg)
    sink = "module-null-sink sink_name={0} sink_properties=device.description={0} format=s16le rate=44100 channels=2"
    argv = [exe, "--daemonize=yes", "--exit-idle-time=-1", "-n", "--load=module-native-protocol-unix"]
    argv += [f"--load={sink.format(n)}" for n in ("perflab_a", "perflab_b")]
    try:
        subprocess.run(argv, env=env, check=True, capture_output=True, timeout=60)
        for _ in range(100):
            if subprocess.run([pactl, "info"], env=env, capture_output=True, check=False).returncode == 0:
                break
            time.sleep(0.1)
        subprocess.run([pactl, "set-default-sink", "perflab_a"], env=env, check=True, capture_output=True)
        yield env
    finally:
        subprocess.run([exe, "--kill"], env=env, capture_output=True, check=False, timeout=30)
        shutil.rmtree(xdg, ignore_errors=True)


def set_default_sink(env: dict[str, str], sink: str) -> None:
    """Make sink the default output of the pulse server env reaches."""
    pactl = shutil.which("pactl")
    assert pactl is not None
    subprocess.run([pactl, "set-default-sink", sink], env=env, check=True, capture_output=True)
