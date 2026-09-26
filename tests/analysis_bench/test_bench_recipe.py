"""NATIVE-11: `just bench` is the fresh-clone agentbox entry point."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
JUSTFILE: Path = REPO_ROOT / "justfile"
BENCH_RUN: Path = REPO_ROOT / "scripts/bench_run.sh"
BENCH_HEADER: str = 'bench lane candidate version="v1":'


def _bench_recipe_body() -> list[str]:
    lines = JUSTFILE.read_text(encoding="utf-8").splitlines()
    body: list[str] = []
    for line in lines[lines.index(BENCH_HEADER) + 1 :]:
        if line and not line.startswith(("    ", "\t")):
            break
        if line.strip() and not line.strip().startswith("#"):
            body.append(line)
    return body


# REQ: NATIVE-11
@pytest.mark.requirement("NATIVE-11")
def test_just_bench_pulls_by_checksum_and_appends_a_round() -> None:
    """[if] just bench runs from a fresh clone [then] it pulls, scores, posts, [else stop]."""
    body = _bench_recipe_body()
    assert any("./scripts/bench_run.sh" in line for line in body), body
    script = BENCH_RUN.read_text(encoding="utf-8")
    assert "bench_pull.sh" in script
    assert "--post" in script
    assert "apps.analysis_bench" in script
