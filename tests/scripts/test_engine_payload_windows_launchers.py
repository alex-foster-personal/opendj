"""The Windows payload launchers carry the same environment contract as the POSIX ones.

[if] a variable the POSIX launcher exports or unsets is missing from the Windows launcher [then] a Windows engine runs half-configured, [else stop]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import build_engine_payload as posix
from scripts import payload_launchers_win as win
from scripts.build_engine_payload_win import windows_system_tool

REPO_ROOT = Path(__file__).resolve().parents[2]
ELECTRON_PLATFORM_TS = REPO_ROOT / "apps" / "desktop" / "electron" / "src" / "platform.ts"


def _posix_exported() -> set[str]:
    names: set[str] = set()
    for line in posix.PAYLOAD_LAUNCHER_PREAMBLE.splitlines():
        match = re.fullmatch(r"export ([A-Z_ ]+)", line.strip())
        if match:
            names.update(match.group(1).split())
    return names


def _posix_unset() -> set[str]:
    return set(re.findall(r"^unset ([A-Z_]+)$", posix.PAYLOAD_LAUNCHER_PREAMBLE, flags=re.MULTILINE))


def _windows_set(template: str) -> set[str]:
    return set(re.findall(r'^(?:if not defined [A-Z_]+ )?set "([A-Z_]+)=[^"]+"', template, flags=re.MULTILINE))


def _windows_unset(template: str) -> set[str]:
    return set(re.findall(r'^set "([A-Z_]+)="$', template, flags=re.MULTILINE))


def test_the_windows_launchers_export_exactly_what_the_posix_ones_export() -> None:
    assert _posix_exported(), "found no exports in the POSIX preamble, so the parity check would pass for nothing"
    assert _windows_set(win.WINDOWS_PAYLOAD_PREAMBLE) == _posix_exported()


def test_the_windows_launchers_unset_exactly_what_the_posix_ones_unset() -> None:
    assert _posix_unset() == {"MDT_REKORDBOX_WRITEBACK_ENABLED"}, "the POSIX unset list changed; update the Windows launcher"
    assert _windows_unset(win.WINDOWS_PAYLOAD_PREAMBLE) == _posix_unset()


def test_each_windows_launcher_runs_the_same_module_as_its_posix_twin() -> None:
    assert "-m apps.engine_core serve %*" in win.ENGINE_LAUNCHER_TEMPLATE
    assert "-m apps.opendj_cli %*" in win.CLI_LAUNCHER_TEMPLATE
    assert win.PYTHON_LAUNCHER_TEMPLATE.rstrip().splitlines()[-2:] == [
        '"%payload%\\runtime\\python.exe" %*',
        "exit /b %ERRORLEVEL%",
    ]


def test_the_launcher_name_is_the_one_the_electron_shell_spawns() -> None:
    shell_source = ELECTRON_PLATFORM_TS.read_text(encoding="utf-8")
    assert f"'{win.LAUNCHER_RELATIVE}'" in shell_source
    assert f"'{posix.LAUNCHER_RELATIVE}'" in shell_source


def test_writing_the_launchers_uses_crlf_and_the_three_names(tmp_path: Path) -> None:
    written = win.write_windows_launchers(tmp_path)
    assert [path.relative_to(tmp_path).as_posix() for path in written] == [
        "bin/opendj-engine.cmd",
        "bin/opendj.cmd",
        "bin/opendj-python.cmd",
    ]
    for path in written:
        raw = path.read_bytes()
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), f"{path.name} must use CRLF only"


def _runtime_like(payload: Path) -> None:
    """A runtime that is a copy of the real interpreter plus the DLLs it loads."""
    runtime = payload / "runtime"
    runtime.mkdir(parents=True)
    base = Path(sys.base_prefix)
    shutil.copy2(base / "python.exe", runtime / "python.exe")
    for dll in [*base.glob("python3*.dll"), *base.glob("vcruntime140*.dll")]:
        shutil.copy2(dll, runtime / dll.name)


@pytest.mark.skipif(sys.platform != "win32", reason="a .cmd launcher is only executed by Windows")
def test_a_windows_launcher_reaches_python_with_the_contract_and_its_arguments(tmp_path: Path) -> None:
    payload = tmp_path / "App Dir & Co" / "payload"
    _runtime_like(payload)
    launcher = win.write_windows_launchers(payload)[2]
    probe = (
        "import json, os, sys; names = ['PYTHONPATH', 'ODJ_AUDIO_BIN', 'ODJ_FFMPEG_BIN', 'OPENDJ_PAYLOAD_MANIFEST', "
        "'MDT_STEM_WORKER_PYTHON', 'MDT_REKORDBOX_WRITEBACK_ENABLED']; "
        "print(json.dumps({'env': {n: os.environ.get(n) for n in names}, 'argv': sys.argv[1:]}))"
    )
    env = {**os.environ, "PYTHONHOME": sys.base_prefix, "MDT_REKORDBOX_WRITEBACK_ENABLED": "1"}
    cmd = windows_system_tool("cmd.exe")
    result = subprocess.run(
        # One string, not a list: list2cmdline would re-escape the quotes cmd.exe /s expects verbatim,
        # which is the same reason the Electron shell sets windowsVerbatimArguments.
        f'"{cmd}" /d /s /c ""{launcher}" -c "{probe}" "one two""',
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout)
    root = str(payload)
    assert seen["argv"] == ["one two"]
    assert seen["env"]["PYTHONPATH"] == f"{root}\\app;{root}\\pylib"
    assert seen["env"]["ODJ_AUDIO_BIN"] == f"{root}\\bin\\odj-audio.exe"
    assert seen["env"]["ODJ_FFMPEG_BIN"] == f"{root}\\bin\\ffmpeg.exe"
    assert seen["env"]["OPENDJ_PAYLOAD_MANIFEST"] == f"{root}\\manifest.json"
    assert seen["env"]["MDT_STEM_WORKER_PYTHON"] == f"{root}\\runtime\\python.exe"
    assert seen["env"]["MDT_REKORDBOX_WRITEBACK_ENABLED"] is None, "writeback must be unset by the launcher"
