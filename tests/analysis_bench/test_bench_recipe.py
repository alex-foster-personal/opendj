"""NATIVE-11: `just bench` is the fresh-clone agentbox entry point."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
JUSTFILE: Path = REPO_ROOT / "justfile"
BENCH_RUN: Path = REPO_ROOT / "scripts/bench_run.sh"
BENCH_HEADER: str = 'bench lane candidate version="v1":'
LANE: str = "probe-lane"
CANDIDATE: str = "probe-candidate"


def _bench_recipe_body() -> list[str]:
    lines = JUSTFILE.read_text(encoding="utf-8").splitlines()
    body: list[str] = []
    for line in lines[lines.index(BENCH_HEADER) + 1 :]:
        if line and not line.startswith(("    ", "\t")):
            break
        if line.strip() and not line.strip().startswith("#"):
            body.append(line)
    return body


def _argv_reaching_uv(tmp_path: Path, *extra: str) -> list[str]:
    """Run the real bench_run.sh in a disposable clone whose bundle is already
    present, with an argv-recording `uv` first on PATH, and return the argv
    the script hands to `uv`."""
    clone = tmp_path / "clone"
    (clone / "scripts").mkdir(parents=True)
    shutil.copy2(BENCH_RUN, clone / "scripts/bench_run.sh")
    bundle = clone / "data/bench" / LANE / "v1"
    bundle.mkdir(parents=True)
    (bundle / "BUNDLE_ID").write_text("probe\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "uv-argv.txt"
    recorder = bin_dir / "uv"
    recorder.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_log}"\n', encoding="utf-8")
    recorder.chmod(recorder.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    subprocess.run(
        ["bash", str(clone / "scripts/bench_run.sh"), LANE, CANDIDATE, "v1", *extra],
        env=env, check=True, capture_output=True, text=True,
    )
    return argv_log.read_text(encoding="utf-8").splitlines()


# REQ: NATIVE-11
@pytest.mark.requirement("NATIVE-11")
def test_just_bench_pulls_by_checksum_and_appends_a_round(tmp_path: Path) -> None:
    """[if] just bench runs from a fresh clone [then] it pulls, scores, posts, [else stop]."""
    body = _bench_recipe_body()
    recipe_calls = [line.split() for line in body if "./scripts/bench_run.sh" in line]
    assert len(recipe_calls) == 1, body
    assert "--no-post" not in recipe_calls[0], recipe_calls[0]
    assert "bench_pull.sh" in BENCH_RUN.read_text(encoding="utf-8")
    argv = _argv_reaching_uv(tmp_path)
    module_at = argv.index("apps.analysis_bench")
    assert argv[:module_at] == ["run", "--no-sync", "python", "-m"], argv
    bench_args = argv[module_at + 1 :]
    expected_head = ["run", "--lane", LANE, "--candidate", CANDIDATE, "--version", "v1"]
    assert bench_args[:7] == expected_head, argv
    assert bench_args[-1] == "--post", argv


# REQ: NATIVE-11
@pytest.mark.requirement("NATIVE-11")
def test_bench_dry_run_does_not_append_a_round(tmp_path: Path) -> None:
    """[if] bench_run.sh gets --no-post [then] apps.analysis_bench gets no --post, [else stop]."""
    argv = _argv_reaching_uv(tmp_path, "--no-post")
    assert "apps.analysis_bench" in argv, argv
    assert "--post" not in argv, argv
