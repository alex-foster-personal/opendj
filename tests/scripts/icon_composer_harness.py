"""Shared actool capability controls for Icon Composer tests (issue #2633)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

from scripts.icon_composer_asset import try_find_actool

NoActoolEnv = Literal["native"] | dict[str, str]

_XCRUN_NO_ACTOOL_SHIM = """#!/bin/sh
if [ "$1" = "--find" ] && [ "$2" = "actool" ]; then
    echo 'xcrun: error: unable to find utility "actool", not a developer tool or in PATH' >&2
    exit 72
fi
exec "{real_xcrun}" "$@"
"""


def path_without_actool_resolver() -> str | None:
    """PATH that cannot resolve actool, with dirname/uname/etc. left reachable.

    ``dmg_preflight.sh`` shells out to plain coreutils (dirname, uname) as well
    as ``xcrun --find actool``. Stripping xcrun's whole directory out of PATH
    used to isolate "no actool" by also taking those coreutils with it, since
    on macOS both live in /usr/bin -- a preflight run under that PATH failed
    before it ever reached the actool check, with 'dirname: command not
    found'. Instead, PREPEND a throwaway shim directory whose only ``xcrun``
    answers ``--find actool`` exactly as a real Xcode-less host's xcrun does
    (stderr message, exit 72) and forwards every other call to the real
    xcrun, plus a second, empty, actool-less directory. Everything else on
    PATH is untouched.
    """
    real_xcrun = shutil.which("xcrun")
    if real_xcrun is None:
        return None
    shim_dir = Path(tempfile.mkdtemp(prefix="no-actool-xcrun-"))
    empty_dir = Path(tempfile.mkdtemp(prefix="no-actool-empty-"))
    xcrun_shim = shim_dir / "xcrun"
    xcrun_shim.write_text(_XCRUN_NO_ACTOOL_SHIM.format(real_xcrun=real_xcrun))
    xcrun_shim.chmod(0o755)
    return os.pathsep.join([str(shim_dir), str(empty_dir), os.environ.get("PATH", "")])


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
