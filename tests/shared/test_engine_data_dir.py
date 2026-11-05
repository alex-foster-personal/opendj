"""The engine data dir rule per OS, and the CLI/MCP lock path derived from it.

Real filesystem and real process environment: environment cases run in a child
process, the cross-language case runs the Electron shell's own TypeScript
module under node. No mocks. See ADR windows-data-dir-and-app-identity.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from apps.shared import engine_origin
from apps.shared.engine_data_dir import (
    ENGINE_IDENTIFIER,
    EngineDataDirError,
    engine_data_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHELL_MODULE = REPO_ROOT / "apps" / "desktop" / "electron" / "src" / "data-dir.ts"
IS_WINDOWS = sys.platform == "win32"


def _child_env(**overrides: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop(engine_origin.LOCK_PATH_ENV, None)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _run_python(code: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


# ----- pure rule, explicit platform ------------------------------------------


def test_windows_is_localappdata_not_roaming() -> None:
    # if the windows data dir is under APPDATA (Roaming) then broken
    got = engine_data_dir(
        platform="win32",
        env={"LOCALAPPDATA": r"C:\Users\dj\AppData\Local", "APPDATA": r"C:\Users\dj\AppData\Roaming"},
        home=Path("/home/dj"),
    )
    assert PureWindowsPath(got) == PureWindowsPath(r"C:\Users\dj\AppData\Local") / ENGINE_IDENTIFIER
    assert "Roaming" not in str(got)


@pytest.mark.parametrize("bad", [None, "", "   ", r"AppData\Local"])
def test_windows_unset_empty_or_relative_localappdata_raises(bad: str | None) -> None:
    # if LOCALAPPDATA is missing/blank/relative and a path is still returned then broken
    env = {} if bad is None else {"LOCALAPPDATA": bad}
    with pytest.raises(EngineDataDirError, match="LOCALAPPDATA"):
        engine_data_dir(platform="win32", env=env, home=Path("/home/dj"))


def test_macos_is_application_support_unchanged() -> None:
    # if the macOS path moves off ~/Library/Application Support/com.opendj.desktop then broken
    got = engine_data_dir(platform="darwin", env={}, home=Path("/Users/dj"))
    assert got.as_posix() == "/Users/dj/Library/Application Support/com.opendj.desktop"


def test_linux_is_xdg_data_home_then_local_share_unchanged() -> None:
    # if Linux stops honoring XDG_DATA_HOME, or the ~/.local/share fallback, then broken
    xdg = engine_data_dir(platform="linux", env={"XDG_DATA_HOME": "/data/xdg"}, home=Path("/home/dj"))
    assert xdg.as_posix() == "/data/xdg/com.opendj.desktop"
    fallback = engine_data_dir(platform="linux", env={}, home=Path("/home/dj"))
    assert fallback.as_posix() == "/home/dj/.local/share/com.opendj.desktop"


def test_unknown_platform_raises() -> None:
    # if an unsupported platform silently gets a guessed path then broken
    with pytest.raises(EngineDataDirError, match="platform"):
        engine_data_dir(platform="plan9", env={}, home=Path("/h"))


# ----- real child process, real environment ----------------------------------


@pytest.mark.skipif(not IS_WINDOWS, reason="POSIX_ONLY: the win32 arm reads the real LOCALAPPDATA only on Windows")
def test_child_process_without_localappdata_exits_nonzero() -> None:
    # if the CLI resolves a lock path on Windows with LOCALAPPDATA unset then broken
    result = _run_python(
        "from apps.shared.engine_origin import lock_path; print(lock_path())",
        _child_env(LOCALAPPDATA=None),
    )
    assert result.returncode != 0
    assert "LOCALAPPDATA" in result.stderr


def test_cli_default_lock_path_is_the_data_dir_lock() -> None:
    # if `opendj status` with no env looks anywhere but <engine_data_dir>/.engine.lock then broken
    local = r"C:\Users\user\AppData\Local" if IS_WINDOWS else "/unused"
    result = _run_python(
        "from apps.shared.engine_origin import lock_path; print(lock_path())",
        _child_env(LOCALAPPDATA=local),
    )
    assert result.returncode == 0, result.stderr
    expected = engine_data_dir(env={**os.environ, "LOCALAPPDATA": local}) / ".engine.lock"
    assert Path(result.stdout.strip()) == expected


def test_lock_path_override_still_wins() -> None:
    # if OPENDJ_LIVE_LOCK_PATH stops overriding the default then broken
    got = engine_origin.lock_path({engine_origin.LOCK_PATH_ENV: "/tmp/other/.engine.lock"})
    assert got == Path("/tmp/other/.engine.lock")


def test_override_needs_no_localappdata() -> None:
    # if an explicit override is blocked by an unset LOCALAPPDATA then broken
    result = _run_python(
        "from apps.shared.engine_origin import lock_path; print(lock_path())",
        _child_env(LOCALAPPDATA=None, OPENDJ_LIVE_LOCK_PATH=str(REPO_ROOT / "x" / ".engine.lock")),
    )
    assert result.returncode == 0, result.stderr


# ----- cross-language: the Electron shell's module agrees --------------------


@pytest.mark.parametrize(
    ("platform", "env_overrides"),
    [
        ("win32", {"LOCALAPPDATA": r"C:\Users\dj\AppData\Local"}),
        ("linux", {"XDG_DATA_HOME": "/data/xdg"}),
        ("linux", {"XDG_DATA_HOME": ""}),
        ("darwin", {}),
    ],
)
def test_python_and_electron_shell_resolve_the_same_path(platform: str, env_overrides: dict[str, str]) -> None:
    # if Python's engine_data_dir() and the shell's resolveEngineDataDir() differ on any OS then broken
    node = shutil.which("node")
    assert node is not None, "node is required for the cross-language check"
    home = "/home/dj" if platform != "darwin" else "/Users/dj"
    script = (
        f"import('{SHELL_MODULE.as_uri()}').then(m => process.stdout.write("
        f"m.resolveEngineDataDir('{ENGINE_IDENTIFIER}', {{platform: '{platform}', "
        f"env: JSON.parse(process.argv[1]), homedir: '{home}', "
        f"appDataDir: '{home}/Library/Application Support'}})))"
    )
    shell = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--no-warnings",
            "--input-type=module",
            "-e",
            script,
            json.dumps(env_overrides),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert shell.returncode == 0, shell.stderr
    python_side = engine_data_dir(platform=platform, env=env_overrides, home=Path(home))
    if platform == "win32":
        assert PureWindowsPath(shell.stdout) == PureWindowsPath(str(python_side))
    else:
        assert shell.stdout == python_side.as_posix()
