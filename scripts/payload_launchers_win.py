"""Windows renderings of the engine payload launchers.

The POSIX launchers in ``build_engine_payload.py`` are the contract between the
desktop shell and the payload: one entry point, the same environment, then the
payload interpreter. Windows gets the same contract as ``.cmd`` files, so the
Electron shell spawns ``bin/opendj-engine.cmd`` exactly where it spawns
``bin/opendj-engine`` elsewhere (``apps/desktop/electron/src/platform.ts``).

Only spellings differ: ``runtime\\python.exe`` for ``runtime/bin/python3``,
``;`` for ``:`` in ``PYTHONPATH``, ``.exe`` on the two native binaries, and
``.cmd`` on the beatgrid runner launcher. The set of variables is checked
against the POSIX preamble by ``tests/scripts/test_engine_payload_windows_launchers.py``,
so the two cannot drift apart.

This module only writes launchers. Staging a Windows runtime, wheels and native
binaries is the rest of the Windows payload recipe and is not done here.

[if] a launcher is written with LF endings or drops a variable the POSIX one sets [then] cmd.exe misparses it or the engine runs half-configured, [else stop]
"""

from __future__ import annotations

from pathlib import Path

LAUNCHER_RELATIVE: str = "bin/opendj-engine.cmd"
CLI_LAUNCHER_RELATIVE: str = "bin/opendj.cmd"
PYTHON_LAUNCHER_RELATIVE: str = "bin/opendj-python.cmd"

WINDOWS_PAYLOAD_PREAMBLE: str = """setlocal DisableDelayedExpansion
rem The payload is one level above bin\\, resolved to an absolute path.
for %%I in ("%~dp0..") do set "payload=%%~fI"

rem One-way safety is absolute. Rekordbox writeback is opt-in through this
rem variable, and an installed app must never be the thing that opts in.
set "MDT_REKORDBOX_WRITEBACK_ENABLED="

rem The engine reads its own identity from here; absent means "repo checkout".
set "OPENDJ_PAYLOAD_MANIFEST=%payload%\\manifest.json"

rem An installed app is one person's local library by definition. The engine
rem refuses an unset MDT_LIBRARY_MODE off darwin, so the launcher says it.
if not defined MDT_LIBRARY_MODE set "MDT_LIBRARY_MODE=local"

rem app first so the engine's own modules win over the dependency tree.
set "PYTHONPATH=%payload%\\app;%payload%\\pylib"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONNOUSERSITE=1"
set "PYTHONSAFEPATH=1"

rem Path to the Desktop-app Google client baked at payload build time.
set "OPENDJ_GOOGLE_OAUTH_CONFIG=%payload%\\app\\apps\\shared\\bundled_google_oauth.json"

rem Local vocal separation and the stems workers run on the payload interpreter.
set "MDT_VOCAL_WORKER_PYTHON=%payload%\\runtime\\python.exe"
set "MDT_VOCAL_WORKER_SCRIPT=%payload%\\app\\scripts\\vocal_region_worker.py"
set "MDT_STEM_WORKER_PYTHON=%payload%\\runtime\\python.exe"

rem The Rust audio engine and ffmpeg the Python engine supervises and runs.
set "ODJ_AUDIO_BIN=%payload%\\bin\\odj-audio.exe"
set "ODJ_FFMPEG_BIN=%payload%\\bin\\ffmpeg.exe"

rem The beatgrid backfill producer runs in its own pinned site, no uv, no network.
set "MDT_BEATGRID_RUNNER_PYTHON=%payload%\\bin\\opendj-beatgrid-python.cmd"
set "MDT_BEATGRID_WEIGHTS=%payload%\\models\\beatgrid\\beat_this-final0.ckpt"

rem The ship Sentry DSN baked at payload build time; path only.
set "OPENDJ_BUNDLED_TELEMETRY=%payload%\\telemetry.json"

"""

ENGINE_LAUNCHER_TEMPLATE: str = (
    "@echo off\n"
    "rem Open DJ engine launcher. THE contract between the desktop shell and the\n"
    "rem payload: the shell knows this path and these flags, nothing else.\n"
    + WINDOWS_PAYLOAD_PREAMBLE
    + '"%payload%\\runtime\\python.exe" -m apps.engine_core serve %*\n'
    + "exit /b %ERRORLEVEL%\n"
)

CLI_LAUNCHER_TEMPLATE: str = (
    "@echo off\n"
    "rem Open DJ CLI and MCP launcher. Same environment contract as opendj-engine.\n"
    + WINDOWS_PAYLOAD_PREAMBLE
    + '"%payload%\\runtime\\python.exe" -m apps.opendj_cli %*\n'
    + "exit /b %ERRORLEVEL%\n"
)

PYTHON_LAUNCHER_TEMPLATE: str = (
    "@echo off\n"
    "rem Open DJ payload interpreter launcher. Same environment contract as\n"
    "rem opendj-engine; runs whatever module or script it is handed.\n"
    + WINDOWS_PAYLOAD_PREAMBLE
    + '"%payload%\\runtime\\python.exe" %*\n'
    + "exit /b %ERRORLEVEL%\n"
)


def _write_cmd(payload_dir: Path, relative: str, template: str) -> Path:
    """Write ``template`` with CRLF line endings, which cmd.exe needs for labels and ``rem``."""
    launcher = payload_dir / relative
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_bytes(template.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    return launcher


def write_windows_launchers(payload_dir: Path) -> tuple[Path, Path, Path]:
    """Write the engine, CLI and interpreter launchers; return them in that order."""
    return (
        _write_cmd(payload_dir, LAUNCHER_RELATIVE, ENGINE_LAUNCHER_TEMPLATE),
        _write_cmd(payload_dir, CLI_LAUNCHER_RELATIVE, CLI_LAUNCHER_TEMPLATE),
        _write_cmd(payload_dir, PYTHON_LAUNCHER_RELATIVE, PYTHON_LAUNCHER_TEMPLATE),
    )
