"""Environment for tests that run ``cargo build`` on apps/audio-engine."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path


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
