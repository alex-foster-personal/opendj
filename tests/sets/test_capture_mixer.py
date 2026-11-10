"""SETREC-05: mixer control capture as v2 events."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apps.sets.control_ids import (
    ENGINE_MIXER_CONTROL_IDS,
    SCRIPTED_MIXER_CONTROL_IDS,
    assert_engine_page_mixer_disjoint,
)

CRATE = Path(__file__).resolve().parents[2] / "apps" / "audio-engine"
RUST_MIXER_TESTS = (
    "mixer_capture_crossfader_sweep_keeps_first_and_last_per_quantum",
    "mixer_capture_eq_from_controller_is_tagged_controller",
    "mixer_capture_scripted_session_covers_every_mixer_control_id",
)


def _cargo_mixer_tests() -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("UNAVAILABLE: no cargo to run engine mixer capture tests")
    env = dict(**__import__("os").environ)
    if shutil.which("g++"):
        env["CXX"] = "g++"
    return subprocess.run(
        ["cargo", "test", "--manifest-path", str(CRATE / "Cargo.toml"), "--lib", "--", *RUST_MIXER_TESTS],
        capture_output=True,
        text=True,
        check=False,
        cwd=CRATE,
        env=env,
    )


@pytest.mark.requirement("SETREC-05")
def test_mixer_control_registry_matches_engine_table() -> None:
    """[if] ENGINE_MIXER ids match scripted list [then] coverage denominator is complete, [else stop]."""
    assert set(SCRIPTED_MIXER_CONTROL_IDS) == set(ENGINE_MIXER_CONTROL_IDS)


@pytest.mark.requirement("SETREC-05")
def test_no_mixer_control_is_logged_by_both_engine_and_page() -> None:
    """[if] engine and page registries overlap [then] fail, [else stop]."""
    assert_engine_page_mixer_disjoint()


@pytest.fixture(scope="module", name="rust_mixer_capture")
def _rust_mixer_capture() -> subprocess.CompletedProcess[str]:
    done = _cargo_mixer_tests()
    if done.returncode != 0 and "fatal error" in done.stderr and "complex" in done.stderr:
        pytest.skip("UNAVAILABLE: odj-audio build needs a C++ stdlib with <complex>")
    return done


@pytest.mark.requirement("SETREC-05")
def test_crossfader_coalescing_keeps_first_and_last_values(
    rust_mixer_capture: subprocess.CompletedProcess[str],
) -> None:
    """[if] rust mixer sweep runs [then] one quantum keeps first+last only, [else stop]."""
    assert rust_mixer_capture.returncode == 0, rust_mixer_capture.stderr[-4000:]
    assert "mixer_capture_crossfader_sweep_keeps_first_and_last_per_quantum ... ok" in rust_mixer_capture.stdout


@pytest.mark.requirement("SETREC-05")
def test_eq_change_from_midi_is_source_controller(
    rust_mixer_capture: subprocess.CompletedProcess[str],
) -> None:
    """[if] engine applies EQ as controller [then] captured source is controller, [else stop]."""
    assert rust_mixer_capture.returncode == 0, rust_mixer_capture.stderr[-4000:]
    assert "mixer_capture_eq_from_controller_is_tagged_controller ... ok" in rust_mixer_capture.stdout


@pytest.mark.requirement("SETREC-05")
def test_scripted_mixer_session_hits_coverage_threshold(
    rust_mixer_capture: subprocess.CompletedProcess[str],
) -> None:
    """[if] scripted mixer session runs [then] every ENGINE_MIXER id is captured, [else stop]."""
    assert rust_mixer_capture.returncode == 0, rust_mixer_capture.stderr[-4000:]
    assert "mixer_capture_scripted_session_covers_every_mixer_control_id ... ok" in rust_mixer_capture.stdout
