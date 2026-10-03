"""The engine half of PERFMODE-15's ratio capture: which pid is the engine root.

Round 8 (PERFMODE-15 spec, ratio family): the Trackify ratios are measured the
same way as PERFMODE-14, which counts the engine and its descendants. This
module reuses PERFMODE-14's identity code rather than restating it:

- `_verify_capture_targets` (capture_library_targets.py): engine reachable and
  clean at the capturing sha, static frontend at the same sha, checkout clean,
  and the engine's own pid from /api/v1/build-info; run again after sampling
  with that pid pinned, so a same-build restart mid-capture refuses the rows.
- `engineRootPids` (renderer-process-sample.ts, via engine_root_pid.mjs): the
  pid is local, owns the engine port, and its command line runs engine code, so
  a port forward or a coinciding pid cannot stand in for the engine.

One check is new because PERFMODE-14 never needed it: its Playwright spec sends
API calls straight to `--engine`, while Trackify's page calls `/api` on
`--frontend`. The engine sampled must be the one the page talks to, so the
frontend's proxied build-info must report the same pid.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.perf.capture_build_identity import _REPO, _http_json
from scripts.perf.capture_library_targets import _verify_capture_targets
from scripts.perf.node_runtime import resolved_node

_ENGINE_ROOT_SCRIPT = _REPO / "scripts" / "perf" / "engine_root_pid.mjs"
_ENGINE_ROOT_TIMEOUT_S = 60


@dataclass(frozen=True)
class EngineTarget:
    origin: str
    pid: int


def _engine_root_pid_reason(engine: str, expected_pid: int) -> str | None:
    """None when PERFMODE-14's `engineRootPids` resolves `engine` to exactly `expected_pid`."""
    proc = subprocess.run(
        # The helper imports a .ts module; type stripping is off by default below Node 22.18.
        [resolved_node(), "--experimental-strip-types", str(_ENGINE_ROOT_SCRIPT), engine, str(expected_pid)],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=_ENGINE_ROOT_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        return f"engine root check refused {engine}: {proc.stderr.strip() or '<empty stderr>'}"
    try:
        roots = json.loads(proc.stdout)["engine_root_pids"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return f"engine root check printed no engine_root_pids: {proc.stdout.strip()!r}"
    if roots != [expected_pid]:
        return f"engine root check resolved {roots!r}, expected [{expected_pid}]"
    return None


def _frontend_engine_pid_reason(frontend: str, engine_pid: int) -> str | None:
    """None when the page's own `/api` reaches the engine being sampled."""
    try:
        status, payload = _http_json("GET", f"{frontend.rstrip('/')}/api/v1/build-info")
    except ConnectionError as exc:
        return f"frontend {frontend} unreachable for build-info: {exc}"
    if status != 200 or not isinstance(payload, dict):
        return f"frontend {frontend} build-info answered HTTP {status}: {payload!r}"
    if payload.get("pid") != engine_pid:
        return (
            f"frontend {frontend} reaches an engine with pid {payload.get('pid')!r}, but the "
            f"sampled engine is pid {engine_pid}: the capture would sample an engine the page "
            "does not use"
        )
    return None


def verify_engine_target(
    engine: str, frontend: str, sha: str, repo_root: Path = _REPO
) -> tuple[EngineTarget | None, str | None]:
    """`(target, None)` once every pre-capture engine gate passes, else `(None, reason)`."""
    reason, _frontend_mode, engine_pid = _verify_capture_targets(
        engine, frontend, sha, repo_root=repo_root
    )
    if reason is not None:
        return None, reason
    if engine_pid is None:
        return None, "PERFMODE-14 target gate passed without an engine pid"
    for check in (
        lambda: _engine_root_pid_reason(engine, engine_pid),
        lambda: _frontend_engine_pid_reason(frontend, engine_pid),
    ):
        reason = check()
        if reason is not None:
            return None, reason
    return EngineTarget(origin=engine, pid=engine_pid), None


def reverify_engine_target(
    target: EngineTarget, frontend: str, sha: str, repo_root: Path = _REPO
) -> str | None:
    """After sampling: the same gates, with the engine pid pinned to the pre-capture one."""
    reason, _frontend_mode, _engine_pid = _verify_capture_targets(
        target.origin, frontend, sha, expected_engine_pid=target.pid, repo_root=repo_root
    )
    if reason is not None:
        return reason
    return _frontend_engine_pid_reason(frontend, target.pid)
