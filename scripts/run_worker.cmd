@echo off
REM Thin wrapper for scripts\vocal_worker_runner.py, invoked via schtasks
REM (HANDOFF.md section 4.2 step 4). SSH-tethered foreground processes die
REM with the connection, so the farm-out MUST be launched detached:
REM
REM   schtasks /create /tn demucs-farm /tr "cmd /c D:\demucs-work\run_worker.cmd" /sc once /st 23:59 /f
REM   schtasks /run /tn demucs-farm
REM
REM Stop cleanly between tracks: type nul > D:\demucs-work\STOP
REM
REM Adjust the paths below to match the target box (RUNBOOK.md "Numbers to
REM remember" has the current bifrost2 values). Nothing here is a hidden
REM default worth guessing at from inside a schtasks-launched cmd window --
REM every path is spelled out explicitly.

setlocal

set REPO_ROOT=D:\music-dj-tools
set WORK_ROOT=D:\demucs-work
set BENCH_PYTHON=D:\tmp\demucs-bench\Scripts\python.exe

REM run_worker's own interpreter override (apps.vocals.cli.run_worker):
REM use the proven bench env (torch+cuda, demucs, htdemucs weights cached)
REM instead of a plain `uv run`, which would resolve CPU-only wheels here
REM (HANDOFF.md section 4.1).
set MDT_VOCAL_WORKER_PYTHON=%BENCH_PYTHON%

"%BENCH_PYTHON%" "%REPO_ROOT%\scripts\vocal_worker_runner.py" ^
    --inbox "%WORK_ROOT%\inbox" ^
    --outbox "%WORK_ROOT%\outbox" ^
    --logs "%WORK_ROOT%\logs" ^
    --device cuda ^
    --gpu-gate ^
    --loop ^
    --poll-seconds 60

endlocal
