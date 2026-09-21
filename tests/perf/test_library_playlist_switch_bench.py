"""Playlist switch latency regression gate (PERF-UI-05, issue #3530)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND = _REPO / "apps" / "webui" / "frontend"
_FIXTURE = _FRONTEND / "tests" / "fixtures" / "library-playlist-switch-bench.json"
_BENCH_CONFIG = "tests/e2e/playwright.playlist-switch-latency.config.ts"
_BUILD_INDEX = _FRONTEND / "build" / "index.html"


def _run_live_playlist_switch_bench() -> dict:
    """Run the Playwright bench and return the measured payload JSON."""
    if not _BUILD_INDEX.is_file():
        subprocess.run(
            ["pnpm", "build"],
            cwd=_FRONTEND,
            check=True,
        )
    env = dict(os.environ)
    # Default bind port is 8713 (issue #3729): agentbox holds 8701 for
    # opendj-release@rb-parity (#3448), so the config default must stay outside
    # that release-engine band.
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
