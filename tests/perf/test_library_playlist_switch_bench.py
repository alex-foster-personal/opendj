"""Playlist switch latency regression gate (PERF-UI-05, issue #3530).

Runs in the e2e workflow's ``extended`` job (frontend build, node_modules and a chromium
present), never in the ci.yml pytest lanes, which ignore this file: it is a full Playwright
bench with a 600 s test timeout and a 240 s webServer timeout, and it ate a whole shard's
1380 s budget on PR #3732. ``tests/scripts/test_ci_playlist_bench_lane.py`` pins the lane.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND = _REPO / "apps" / "webui" / "frontend"
_FIXTURE = _FRONTEND / "tests" / "fixtures" / "library-playlist-switch-bench.json"
_BENCH_CONFIG = "tests/e2e/playwright.playlist-switch-latency.config.ts"
_BUILD_INDEX = _FRONTEND / "build" / "index.html"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_live_playlist_switch_bench() -> dict:
    """Run the Playwright bench and return the measured payload JSON."""
    if not _BUILD_INDEX.is_file():
        subprocess.run(
            ["pnpm", "build"],
            cwd=_FRONTEND,
            check=True,
        )
    env = dict(os.environ)
    # agentbox-14 and agentbox-15 share one host, so two shards (or a leaked
    # engine from an earlier run) can both want the config's default port:
    # "http://127.0.0.1:8701/api/v1/health is already used" on main at
    # 2f0b917e (Mon 21 Sep 2026). An ephemeral port is never in the config's
    # RESERVED_PORTS table, which tops out below 10000. The config default
    # itself moved 8701 -> 8713 (issue #3729) for operators running the bench
    # by hand, since agentbox holds 8701 for opendj-release@rb-parity (#3448);
    # the per-run port here is what keeps concurrent shards apart.
    env.setdefault("PLAYLIST_SWITCH_BENCH_PORT", str(_free_port()))
    env.setdefault("PLAYLIST_SWITCH_BENCH_SAMPLES", "10")
    env["PLAYLIST_SWITCH_BENCH_UPDATE_FIXTURE"] = "1"
    env["PLAYLIST_SWITCH_BENCH_OUT"] = str(_FIXTURE)
    subprocess.run(
        [
            "pnpm",
            "exec",
            "playwright",
            "test",
            "--config",
            _BENCH_CONFIG,
        ],
        cwd=_FRONTEND,
        check=True,
        env=env,
    )
    return json.loads(_FIXTURE.read_text())


@pytest.mark.requirement("PERF-UI-05")
@pytest.mark.slow  # full Playwright bench: never in the CI fast tier (scripts/pytest_fast_tier.py)
def test_playlist_switch_bench_meets_post_fix_caps() -> None:
    """[if] live bench p50 exceeds caps [then] gate fails, [else stop]."""
    payload = _run_live_playlist_switch_bench()
    thresholds = payload["thresholds_p50_ms"]
    post_fix = payload["post_fix"]
    assert isinstance(payload.get("sha"), str) and payload["sha"]
    assert isinstance(post_fix, dict)
    for metric, cap in thresholds.items():
        measured = post_fix[metric]["p50"]
        assert measured <= cap, (
            f"{metric} p50 {measured}ms exceeds cap {cap}ms"
        )
