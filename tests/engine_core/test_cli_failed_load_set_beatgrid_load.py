"""Load and contract checks for cli serve tests hardened under #11380."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from apps.shared import platform_paths
from tests.rust_build_env import bindgen_env, build_audio_engine

CRATE = platform_paths.PROJECT_ROOT / "apps" / "audio-engine"
CLI_RS = CRATE / "tests" / "cli.rs"
FAILED_LOAD = "a_failed_load_refuses_the_commands_queued_behind_it"
SET_BEATGRID = "set_beatgrid_reaches_a_loaded_deck_without_reloading"
PARALLEL_RUNS = 12


def _cargo_env() -> dict[str, str]:
    env = bindgen_env(os.environ)
    env.setdefault("CXX", "g++")
    env.setdefault("CC", "gcc")
    if sys.platform == "linux":
        env.setdefault("RUSTFLAGS", "-C link-arg=-L/usr/lib/gcc/x86_64-linux-gnu/13")
    return env


def _run_cli_test(name: str) -> None:
    proc = subprocess.run(
        ["cargo", "test", "--locked", "--test", "cli", name, "--", "--exact"],
        cwd=CRATE,
        env=_cargo_env(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"cargo test {name} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")


@pytest.fixture(scope="module")
def built_audio_engine() -> Path:
    """Build odj-audio once for repeated cargo test invocations."""
    return build_audio_engine(CRATE)


def test_cli_hardened_tests_green_under_parallel_cargo_on_linux(
    built_audio_engine: Path,
) -> None:
    """[if] parallel cargo test on a loaded runner [then] both cli cases stay green, [else stop]."""
    _ = built_audio_engine
    jobs = [FAILED_LOAD] * (PARALLEL_RUNS // 2) + [SET_BEATGRID] * (PARALLEL_RUNS // 2)
    with ThreadPoolExecutor(max_workers=PARALLEL_RUNS) as pool:
        futures = [pool.submit(_run_cli_test, name) for name in jobs]
        for fut in as_completed(futures):
            fut.result()


def test_failed_load_keeps_fifo_open_until_queued_play_is_read() -> None:
    """[if] queued play is sent [then] fifo is fed only after crossfader read proof, [else stop]."""
    text = CLI_RS.read_text(encoding="utf-8")
    start = text.index("fn a_failed_load_refuses_the_commands_queued_behind_it")
    block = text[start : start + 2400]
    assert '"id": "queued"' in block
    assert '"id": "read"' in block and "crossfader" in block
    assert "std::fs::write(&fifo" in block
    read_pos = block.index('"id": "read"')
    fifo_pos = block.index("std::fs::write(&fifo")
    assert read_pos < fifo_pos
    assert "from_millis(200)" not in block


def test_set_beatgrid_keeps_jump_at_400ms_under_deadline_waits() -> None:
    """[if] load and set_beatgrid queue on stdin [then] beat jump settles at 400ms, [else stop]."""
    text = CLI_RS.read_text(encoding="utf-8")
    assert "struct ServeLines" in text
    assert "SERVE_WAIT" in text
    assert "recv_timeout" in text
    start = text.index("fn set_beatgrid_reaches_a_loaded_deck_without_reloading")
    block = text[start : start + 2800]
    assert "set_beatgrid" in block
    assert "beatgrid_ms" in block and "400" in block
    assert "(pos - 400" in block and "1e-6" in block
    _run_cli_test(SET_BEATGRID)


def test_loads_in_flight_eof_waits_on_fifo_not_fixed_sleep() -> None:
    """[if] stdin EOF with fifo decode open [then] fifo feed has no fixed sleep, [else stop]."""
    text = CLI_RS.read_text(encoding="utf-8")
    start = text.index("fn loads_in_flight_at_eof_still_get_results")
    block = text[start : start + 1800]
    assert "drop(stdin)" in block
    assert "from_millis(200)" not in block
    assert "results_for" in block
