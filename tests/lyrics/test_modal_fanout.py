"""Modal ASR/align fan-out pins (operational-plan section 12, R6).

Regression lines (one-line if/then, house format):
- if either Modal spike stops dispatching jobs via .starmap then the stage is
  a serial job loop and the 5-min/100-track wall target dies - broken (R6)
- if MAX_CONTAINERS drops below the R6 floor then 100 stem-ready tracks queue
  behind too few containers - broken
- if MAX_CONTAINERS stops being wired into @app.cls then the cap is a dead
  constant and Modal falls back to its own default - broken
- if a per-job .remote() call appears then someone reintroduced the serial
  dispatch pattern the starmap port removed - broken
- if order_outputs=False disappears then results stream in submission order
  and one slow track head-of-line-blocks every progress line - broken

Textual on purpose: modal cannot be imported in the repo venv (heavy-ML house
rule), so these read the scripts as text - the exact pattern of the driver's
_assert_iso3_pins preflight in apps/lyrics/batch.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
ASR_SPIKE: Path = REPO_ROOT / "scripts" / "modal_asr_spike.py"
ALIGN_SPIKE: Path = REPO_ROOT / "scripts" / "modal_align_spike.py"
# R6 floor, ratchet-only: raise it with evidence, never lower it silently.
MIN_MAX_CONTAINERS: int = 20

# (script, functions that must dispatch via starmap)
FANOUT_ENTRYPOINTS: list[tuple[Path, list[str]]] = [
    (ASR_SPIKE, ["_run_jobs"]),
    (ALIGN_SPIKE, ["cmd_align", "cmd_parity", "cmd_align_files"]),
]


def _function_body(text: str, name: str) -> str:
    chunks = [c for c in text.split("\ndef ") if c.startswith(f"{name}(")]
    assert len(chunks) == 1, f"top-level function {name} not found exactly once"
    return chunks[0]


@pytest.mark.parametrize("script", [ASR_SPIKE, ALIGN_SPIKE], ids=lambda p: p.name)
def test_container_cap_meets_the_r6_floor_and_is_wired(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    m = re.search(r"^MAX_CONTAINERS: int = (\d+)$", text, re.MULTILINE)
    assert m is not None, f"{script.name}: MAX_CONTAINERS declaration missing"
    assert int(m.group(1)) >= MIN_MAX_CONTAINERS, (
        f"{script.name}: MAX_CONTAINERS {m.group(1)} below the R6 floor "
        f"{MIN_MAX_CONTAINERS}")
    assert "max_containers=MAX_CONTAINERS" in text, (
        f"{script.name}: MAX_CONTAINERS is not wired into @app.cls")


@pytest.mark.parametrize(("script", "functions"), FANOUT_ENTRYPOINTS,
                         ids=lambda v: v.name if isinstance(v, Path) else "fns")
def test_every_dispatch_entrypoint_uses_starmap(
    script: Path, functions: list[str]
) -> None:
    text = script.read_text(encoding="utf-8")
    for name in functions:
        body = _function_body(text, name)
        assert ".starmap(" in body, f"{script.name}:{name} lost its starmap fan-out"
        assert "order_outputs=False" in body, (
            f"{script.name}:{name} must stream results as they complete")


def test_asr_subcommands_route_through_the_fanned_out_runner() -> None:
    text = ASR_SPIKE.read_text(encoding="utf-8")
    for name in ("cmd_transcribe", "cmd_transcribe_files"):
        assert "_run_jobs(" in _function_body(text, name), (
            f"{name} no longer dispatches via _run_jobs")


@pytest.mark.parametrize("script", [ASR_SPIKE, ALIGN_SPIKE], ids=lambda p: p.name)
def test_no_serial_per_job_remote_calls(script: Path) -> None:
    assert ".remote(" not in script.read_text(encoding="utf-8"), (
        f"{script.name}: a per-job .remote() call is the serial dispatch "
        f"pattern R6 removed")
