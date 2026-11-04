"""Shared fixtures for the odj-live tests: the built binary and librosa.

A host without cargo, librosa or soundfile reports UNAVAILABLE (a skip naming
the gap). The contracts job, which builds the crate, sets
``MDT_REQUIRE_LIVE_CORE_BUILD=1``, and there every such gap is a failure.
That includes the audio stack: ``requires_audio_stack`` would make
tests/conftest.py skip a test before any fixture runs, so the tests apply it
through :data:`AUDIO_STACK_MARKS`, which is empty under the flag.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import NoReturn

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Cosine of odj-live's mean chroma against librosa's chroma_cqt mean that
# every parity test (synthetic and the encoded acceptance media) must reach.
COSINE_FLOOR = 0.98
CRATE = REPO_ROOT / "apps" / "live-core"
REQUIRE_ENV = "MDT_REQUIRE_LIVE_CORE_BUILD"


def require_build() -> bool:
    return os.environ.get(REQUIRE_ENV) == "1"


#: The audio-stack skip marker, or nothing when the require flag is set, so a
#: missing librosa or soundfile reaches the fixture below and fails there.
AUDIO_STACK_MARKS: list[pytest.MarkDecorator] = [] if require_build() else [pytest.mark.requires_audio_stack]


def unavailable(reason: str) -> NoReturn:
    if require_build():
        pytest.fail(f"{reason}, and {REQUIRE_ENV}=1 requires it")
    pytest.skip(f"UNAVAILABLE: {reason}; the contracts job runs this with {REQUIRE_ENV}=1")
    # pytest.skip never returns, but pytest is not importable in the quality
    # gate's mypy env, which would otherwise read this as an implicit return.
    raise AssertionError("pytest.skip returned")


@pytest.fixture(scope="session")
def librosa_module() -> ModuleType:
    # librosa.load reads files through soundfile, so both are the reference.
    try:
        import soundfile  # noqa: F401
    except (ImportError, OSError) as exc:
        unavailable(f"soundfile is not importable here ({exc}), so librosa cannot load audio")
    try:
        import librosa
    except ImportError as exc:
        unavailable(f"librosa is not importable here ({exc}), so there is no reference chroma")
    return librosa


@pytest.fixture(scope="session")
def odj_live() -> Path:
    cargo = shutil.which("cargo")
    if cargo is None:
        unavailable("no cargo here, so odj-live cannot be built from this checkout")
    result = subprocess.run(
        [
            cargo,
            "build",
            "--release",
            "--locked",
            "--bin",
            "odj-live",
            "--message-format=json",
            "--manifest-path",
            str(CRATE / "Cargo.toml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"cargo build of odj-live exited {result.returncode}:\n{result.stderr[-4000:]}")
    # Cargo reports where it put the binary, which honors CARGO_TARGET_DIR and
    # any target directory in the Cargo configuration.
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("reason") == "compiler-artifact" and message.get("target", {}).get("name") == "odj-live" and message.get("executable"):
            return Path(message["executable"])
    # Same reason as unavailable(): pytest.fail is untyped in the gate's mypy env,
    # so without a raise here mypy reads this fixture as an implicit return.
    raise AssertionError("cargo build of odj-live reported no executable for the odj-live target")
