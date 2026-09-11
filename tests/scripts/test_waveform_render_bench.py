"""waveform_render_bench measurement lane contract tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from apps.webui.server import rb_vendor

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/bench/waveform_render_bench.py"


@pytest.mark.skipif(
    rb_vendor._WAVEFORM_NATIVE is not None,
    reason="native extension installed; real UNKNOWN path not observable on this host",
)
def test_waveform_render_bench_unknown_without_native() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 3, completed.stderr


def test_waveform_render_bench_writes_medians(tmp_path: Path) -> None:
    wheel_build = subprocess.run(["make", "waveform-native-wheel"], cwd=REPO, check=False)
    if wheel_build.returncode != 0:
        pytest.skip("waveform native wheel build failed on this host")
    out = tmp_path / "waveform-render.json"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out), "--iterations", "3"],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 3:
        pytest.skip(completed.stderr.strip() or "native extension unavailable")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["samples"]["median_cpu_ms"] > 0
    assert payload["samples"]["median_requests_per_second"] > 0
    assert payload["host"]
