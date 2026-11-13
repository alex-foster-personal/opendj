"""Load and contract checks for the midi_page_lines ws integration test (#11378)."""

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
WS_RS = CRATE / "tests" / "ws.rs"
TEST_NAME = "midi_page_lines_reach_every_client_and_the_result_only_its_sender"
PARALLEL_RUNS = 12


def _cargo_env() -> dict[str, str]:
    """Cargo env with GNU compiler defaults on Linux only, so MSVC and Apple hosts keep their own toolchain."""
    env = bindgen_env(os.environ)
    if sys.platform == "linux":
        env.setdefault("CXX", "g++")
        env.setdefault("CC", "gcc")
        env.setdefault("RUSTFLAGS", "-C link-arg=-L/usr/lib/gcc/x86_64-linux-gnu/13")
    return env


def _run_midi_page_lines() -> None:
    """Run the ws integration test once; raise on failure."""
    proc = subprocess.run(
        [
            "cargo",
            "test",
            "--locked",
            "--test",
            "ws",
            TEST_NAME,
            "--",
            "--exact",
        ],
        cwd=CRATE,
        env=_cargo_env(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"cargo test {TEST_NAME} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")


@pytest.fixture(scope="module")
def built_audio_engine() -> Path:
    """Build odj-audio once for repeated cargo test invocations."""
    return build_audio_engine(CRATE)


def test_midi_page_lines_green_under_parallel_cargo_on_linux(
    built_audio_engine: Path,
) -> None:
    """[if] parallel cargo test on a loaded runner [then] midi_page_lines stays green, [else stop]."""
    _ = built_audio_engine
    with ThreadPoolExecutor(max_workers=PARALLEL_RUNS) as pool:
        futures = [pool.submit(_run_midi_page_lines) for _ in range(PARALLEL_RUNS)]
        for fut in as_completed(futures):
            fut.result()


def test_midi_page_lines_waits_use_deadline_sliced_reads() -> None:
    """[if] page lines are slow to reach stdio or socket b [then] waits use deadlines not blocking next, [else stop]."""
    text = WS_RS.read_text(encoding="utf-8")
    assert "const READ_SLICE" in text
    assert "fn until(ws: &mut Socket" in text
    assert "try_next(ws)" in text
    assert "while Instant::now() < deadline" in text


def test_midi_page_lines_drains_idle_clients_while_waiting_on_stdio() -> None:
    """[if] stdio waits on the page line [then] sockets a and b stay drained, [else stop]."""
    text = WS_RS.read_text(encoding="utf-8")
    marker = "stdio is a client like any other"
    start = text.index(marker)
    block = text[start : start + 1200]
    assert "try_next(&mut b)" in block
    assert "try_next(&mut a)" in block
    assert "saturating_duration_since" in block


def test_midi_page_lines_sender_isolation_stays_on_hot_id() -> None:
    """[if] the sender gets the midi page action [then] only the sender sees id hot, [else stop]."""
    text = WS_RS.read_text(encoding="utf-8")
    assert 'v["id"] == "hot"' in text
    assert 'v["id"] != "hot"' in text
    assert 'b_kept.iter().all(|v| v["id"] != "hot")' in text
    _run_midi_page_lines()


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_cargo_env_leaves_compiler_choice_to_cargo_off_linux(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    """[if] the host is not Linux and CC/CXX are unset [then] no GNU compiler is forced on cargo, [else stop]."""
    monkeypatch.delenv("CC", raising=False)
    monkeypatch.delenv("CXX", raising=False)
    monkeypatch.delenv("RUSTFLAGS", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    env = _cargo_env()
    assert "CC" not in env
    assert "CXX" not in env
    assert "RUSTFLAGS" not in env


def test_cargo_env_sets_gnu_defaults_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the host is Linux and CC/CXX are unset [then] gcc/g++ defaults are set, [else stop]."""
    monkeypatch.delenv("CC", raising=False)
    monkeypatch.delenv("CXX", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    env = _cargo_env()
    assert env["CC"] == "gcc"
    assert env["CXX"] == "g++"
