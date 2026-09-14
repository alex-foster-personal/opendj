"""Shared actool capability controls for Icon Composer tests (issue #2633)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from scripts.icon_composer_asset import try_find_actool

NoActoolEnv = Literal["native"] | dict[str, str]


def path_without_actool_resolver() -> str | None:
    """PATH with every directory that provides xcrun removed."""
    xcrun = shutil.which("xcrun")
    if xcrun is None:
        return None
    xcrun_dir = str(Path(xcrun).parent)
    kept = [
        part
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part and part != xcrun_dir
    ]
    if len(kept) == len(os.environ.get("PATH", "").split(os.pathsep)):
        return None
    return os.pathsep.join(kept)


def require_no_actool_env() -> NoActoolEnv:
    """Return a subprocess env that cannot resolve actool, or ``native`` when absent."""
    if try_find_actool() is None:
        return "native"
    path = path_without_actool_resolver()
    if path is None:
        msg = "UNAVAILABLE: xcrun is not on PATH, so actool isolation cannot be measured"
        raise RuntimeError(msg)
    env = dict(os.environ)
    env["PATH"] = path
    return env


def run_icon_composer_cli(
    *args: str,
    cwd: Path,
    no_actool: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m scripts.icon_composer_asset`` with optional actool isolation."""
    cmd = [sys.executable, "-m", "scripts.icon_composer_asset", *args]
    env: dict[str, str] | None = None
    if no_actool:
        cap = require_no_actool_env()
        if cap != "native":
            env = cap
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env=env,
    )
