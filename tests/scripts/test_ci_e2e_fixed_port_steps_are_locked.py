"""Every e2e step that binds a fixed loopback port runs under the host lock.

Eight e2e runners share one host (agentbox). Suites that claim ports through
``apps.webui.port_config`` get a per-runner lane (#1304, #1313); suites whose
config pins a PORT constant do not, so two jobs reaching that step at once
collide. ``webkit-deckload`` (8690) has run under ``scripts/ci_host_lock.sh``
since the pool widened; the comment-hotkey gate (vite 5321, API 8696) did not,
and trunk 79b6a174a's e2e died Sun 6 Sep 2026 with
``http://127.0.0.1:8696/api/v1/health is already used`` under another job's
copy of the same step.

Regression lines:
  - if a fixed-port e2e step runs without the host lock then two jobs on one
    host collide on that port and the second reds
  - if two steps whose configs share a port hold DIFFERENT lock names then the
    lock serializes neither against the other and the collision returns
  - if a config gains a fixed port without its step gaining the lock then the
    same collision returns under a new name
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
E2E_DIR = REPO_ROOT / "apps" / "webui" / "frontend" / "tests" / "e2e"

#: A loopback port pinned as a constant. Lane-claimed suites never spell one.
FIXED_PORT = re.compile(r"(?:PORT[A-Za-z_]*\s*=\s*|127\.0\.0\.1:|localhost:)(\d{4,5})\b")
#: ``from './vite.x.config'`` (extensionless) or ``--config tests/e2e/vite.x.config.ts``.
LOCAL_CONFIG_REF = re.compile(r"(?:from\s+['\"]\./|tests/e2e/)([\w.-]+\.config)(?:\.ts)?['\"\s]")
STEP_CONFIG = re.compile(r"--config\s+tests/e2e/([\w.-]+\.config\.ts)")
LOCK_NAME = re.compile(r"scripts/ci_host_lock\.sh\"?\s+([\w-]+)\s")


def _fixed_ports(config_name: str) -> frozenset[str]:
    """Ports pinned by the named config or any local config it references."""
    seen: set[Path] = set()
    ports: set[str] = set()
    todo = [E2E_DIR / config_name]
    while todo:
        path = todo.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        text = path.read_text(encoding="utf-8")
        ports.update(FIXED_PORT.findall(text))
        todo.extend(E2E_DIR / f"{stem}.ts" for stem in LOCAL_CONFIG_REF.findall(text))
    return frozenset(ports)


def _playwright_steps() -> list[tuple[str, str, str]]:
    """(job, step name, run) for every step that names a tests/e2e playwright config."""
    doc = yaml.safe_load(E2E.read_text(encoding="utf-8"))
    return [
        (job, step.get("name", "?"), step["run"])
        for job, spec in doc["jobs"].items()
        for step in spec["steps"]
        if STEP_CONFIG.search(step.get("run") or "")
    ]


def test_the_probe_sees_ports_through_the_vite_import() -> None:
    """if the port walk misses the extensionless vite import then every gate looks port-free"""
    assert {"5321", "8696"} <= _fixed_ports("playwright.comment-hotkey-gate.config.ts")
    assert _fixed_ports("playwright.stretch-quality.config.ts") == frozenset()


def test_every_fixed_port_e2e_step_runs_under_the_host_lock() -> None:
    """if a fixed-port step drops the lock then eight runners collide on its port"""
    unlocked = []
    checked = 0
    for job, name, run in _playwright_steps():
        config = STEP_CONFIG.search(run).group(1)
        if not _fixed_ports(config):
            continue
        checked += 1
        if not LOCK_NAME.search(run):
            unlocked.append(f"{job}/{name}: {config}")
    assert checked >= 4, f"expected the deckload, hermetic and extended fixed-port steps, saw {checked}"
    assert not unlocked, f"fixed-port steps without scripts/ci_host_lock.sh: {unlocked}"


def test_steps_that_share_a_port_share_a_lock_name() -> None:
    """if two configs on one port take different locks then neither waits for the other"""
    lock_by_port: dict[str, set[str]] = defaultdict(set)
    for _job, _name, run in _playwright_steps():
        config = STEP_CONFIG.search(run).group(1)
        lock = LOCK_NAME.search(run)
        if lock is None:
            continue
        for port in _fixed_ports(config):
            lock_by_port[port].add(lock.group(1))
    split = {port: sorted(locks) for port, locks in lock_by_port.items() if len(locks) > 1}
    assert lock_by_port, "no locked fixed-port step found; the probe is broken"
    assert not split, f"one port, several lock names: {split}"
