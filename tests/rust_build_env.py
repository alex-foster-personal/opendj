"""Build odj-audio (apps/audio-engine) for the tests that drive the real binary.

The pytest shard hosts are not all provisioned for this crate: signalsmith-stretch
runs bindgen, which needs libclang, and some shard runners have none. Those hosts
report UNAVAILABLE rather than a verdict; the contracts job, whose Rust step
already builds this crate, reruns these tests with ``MDT_REQUIRE_AUDIO_ENGINE_BUILD=1``
so the same gap fails there instead of skipping.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

REQUIRE_ENV = "MDT_REQUIRE_AUDIO_ENGINE_BUILD"
# bindgen's own panic text when clang-sys finds no libclang shared library.
NO_LIBCLANG = "Unable to find libclang"
# alsa-sys's build script when pkg-config finds no ALSA development files (Linux).
NO_ALSA = "`alsa` required by crate `alsa-sys`"


def bindgen_env(environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` with gcc's builtin headers added for bindgen, as ci.yml's cargo step does.

    signalsmith-stretch runs bindgen, and the CI runners' libclang ships without
    its resource headers ('stddef.h' file not found). Appended, so flags already
    set are kept; left alone where gcc has no include dir to give.
    """
    env = dict(environ)
    gcc = shutil.which("gcc")
    if gcc is None:
        return env
    include = subprocess.run(
        [gcc, "-print-file-name=include"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if include and Path(include).is_absolute() and Path(include).is_dir():
        extra = env.get("BINDGEN_EXTRA_CLANG_ARGS", "")
        env["BINDGEN_EXTRA_CLANG_ARGS"] = f"{extra} -I{include}".strip()
    return env


def _unavailable(reason: str) -> None:
    """Skip as a capability report, or fail where the build is required."""
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"{reason}, and {REQUIRE_ENV}=1 requires the build")
    pytest.skip(f"UNAVAILABLE: {reason}; the contracts job runs this with {REQUIRE_ENV}=1")


def build_audio_engine(crate: Path, *, features: tuple[str, ...] = ()) -> Path:
    """``cargo build`` odj-audio in ``crate`` and return the debug binary.

    Rebuilt every call (cargo does nothing when it is fresh) so a stale binary
    can't answer. Only a missing cargo or libclang (or, for a ``features``
    build, ALSA's headers) is UNAVAILABLE; any other build failure fails the
    test with cargo's stderr. A ``features`` build gets its own target dir, so
    it never overwrites the default build other tests run concurrently.
    """
    if shutil.which("cargo") is None:
        _unavailable("no cargo here, so odj-audio cannot be built from this checkout")
    target = crate / "target"
    extra: list[str] = []
    if features:
        target = target / ("features-" + "-".join(features))
        extra = ["--features", ",".join(features), "--target-dir", str(target)]
    result = subprocess.run(
        [
            "cargo",
            "build",
            "--quiet",
            "--bin",
            "odj-audio",
            "--manifest-path",
            str(crate / "Cargo.toml"),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=bindgen_env(os.environ),
    )
    if result.returncode != 0:
        if NO_LIBCLANG in result.stderr:
            _unavailable("no libclang here, so bindgen cannot build signalsmith-stretch")
        if features and NO_ALSA in result.stderr:
            _unavailable("no ALSA development files here, so the device feature cannot build")
        pytest.fail(
            f"cargo build of odj-audio exited {result.returncode}:\n{result.stderr[-4000:]}"
        )
    return target / "debug" / ("odj-audio.exe" if sys.platform == "win32" else "odj-audio")
