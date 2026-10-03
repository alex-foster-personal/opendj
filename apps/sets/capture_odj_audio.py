"""The odj-audio capture backend (SET-11): the bundled engine records the REC input.

The installed app bundles no ffmpeg (#4766), so ``odj-audio`` (build feature
``device``) lists inputs (``input-devices``) and records one as rolling WAV
segments (``record``); see ``docs/decisions/*-set-recording-without-ffmpeg.md``.
:mod:`apps.sets.capture` picks between this and ffmpeg.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO

from apps.shared.odj_audio_binary import BIN_ENV, EXE_NAME, REPO_TARGET, OdjAudioUnavailable, find_binary

from .capture_types import (
    CaptureBackend,
    CaptureState,
    CaptureStateName,
    CaptureUnavailable,
    InputDevice,
    is_loopback_name,
)

LIST_DEVICES_TIMEOUT_S = 10.0
VERSION_PROBE_TIMEOUT_S = 10.0


def _capture_refusal(exe: Path, run: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    """Why ``exe`` cannot capture, from ``exe version``; "" when it can."""
    try:
        result = run(
            [str(exe), "version"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=VERSION_PROBE_TIMEOUT_S,
        )
        version = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return f"{exe} version failed: {exc}"
    if not isinstance(version, dict):
        stderr = (result.stderr or "").strip()[-200:] or "no stderr"
        return f"{exe} version exited {result.returncode} without a version object: {stderr}"
    if version.get("capture") is not True:
        return f"{exe} was built without audio input (feature device)"
    return ""


def _repo_builds(repo_root: Path) -> list[Path]:
    """Every local cargo build of odj-audio, newest first."""
    found = [
        p
        for profile in ("release", "debug")
        if (p := repo_root / REPO_TARGET / profile / EXE_NAME).is_file() and os.access(p, os.X_OK)
    ]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def odj_audio_backend(
    environ: Mapping[str, str],
    repo_root: Path,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[CaptureBackend | None, str]:
    """A capture-capable odj-audio, or None and why not.

    An ``ODJ_AUDIO_BIN`` (the installed app) that cannot capture raises
    :class:`CaptureUnavailable` instead: the app must never quietly record
    through some other ffmpeg than the one it does not ship. In a checkout,
    every local build is asked, newest first, since the newest may be a
    default-features build while an older one was built with ``device``.
    """
    if environ.get(BIN_ENV, "").strip():
        try:
            binary = find_binary(environ, repo_root)
        except OdjAudioUnavailable as exc:
            raise CaptureUnavailable(f"cannot record set audio: {exc}") from exc
        reason = _capture_refusal(binary.path, run)
        if reason:
            raise CaptureUnavailable(f"cannot record set audio: {reason}")
        return CaptureBackend("odj-audio", str(binary.path)), ""
    builds = _repo_builds(repo_root)
    if not builds:
        try:
            find_binary(environ, repo_root)
        except OdjAudioUnavailable as exc:
            return None, str(exc)
    reasons = []
    for exe in builds:
        reason = _capture_refusal(exe, run)
        if not reason:
            return CaptureBackend("odj-audio", str(exe)), ""
        reasons.append(reason)
    return None, "; ".join(reasons)


def list_odj_audio_inputs(
    exe: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> list[InputDevice]:
    try:
        result = runner(
            [exe, "input-devices"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=LIST_DEVICES_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureUnavailable(f"listing audio inputs with {exe} failed: {exc}") from exc
    devices = _parse_odj_audio_devices(result.stdout) if result.returncode == 0 else None
    if devices is None:
        tail = (result.stderr or "").strip()[-300:] or "no stderr"
        raise CaptureUnavailable(f"{exe} input-devices exited {result.returncode} without a device list: {tail}")
    return devices


def _parse_odj_audio_devices(stdout: str) -> list[InputDevice] | None:
    """The ``{"devices": [{"index", "name", ...}]}`` line, or None if it is not one."""
    try:
        rows = json.loads(stdout)["devices"]
        return [
            InputDevice(index=int(row["index"]), name=str(row["name"]), loopback=is_loopback_name(str(row["name"])))
            for row in rows
        ]
    except (ValueError, KeyError, TypeError):
        return None


def build_record_argv(
    exe: str,
    device_idx: int,
    output_dir: Path,
    *,
    segment_time_s: int,
    device_name: str | None = None,
) -> list[str]:
    """The odj-audio argv for rolling WAV capture of one input.

    By exact ``device_name`` when known, so an input plugged in between the
    listing and the start cannot move REC onto another one; else by
    ``device_idx``, a place in ``odj-audio input-devices``.
    """
    pick = ["--device", device_name] if device_name else ["--device-index", str(device_idx)]
    return [exe, "record", "--dir", str(output_dir), *pick, "--segment-seconds", str(segment_time_s)]


_STATE_OF_LINE: dict[str, CaptureStateName] = {
    "waiting": "waiting_permission",
    "recording": "recording",
    "stopped": "stopped",
}


def follow_record_output(stdout: IO[bytes], log_fh: IO[bytes], state: CaptureState) -> threading.Thread:
    """Read ``odj-audio record``'s JSON lines into ``state``, copying them to the log.

    ``{"waiting": ...}`` is the macOS microphone prompt, ``{"recording": ...}``
    audio being written, ``{"stopped": ...}`` a clean stop. Output ending any
    other way is a failed capture.
    """

    def _run() -> None:
        for raw in stdout:
            if not log_fh.closed:
                log_fh.write(raw)
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict):
                for key, value in _STATE_OF_LINE.items():
                    if key in line:
                        state.set(value)
        if state.value != "stopped":
            state.set("failed")

    thread = threading.Thread(target=_run, name="odj-audio-record-output", daemon=True)
    thread.start()
    state.reader = thread
    return thread


def stop_odj_audio(proc: subprocess.Popen, timeout: float) -> int:
    """Close its stdin (the stop signal), wait, then kill; its return code."""
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except OSError:
        pass  # already gone: wait() below reads its exit
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Its header is rewritten every second, so even this leaves a WAV
        # that plays up to the last second written.
        proc.kill()
        proc.wait(timeout=2.0)
    return int(proc.returncode or 0)


__all__ = [
    "build_record_argv",
    "follow_record_output",
    "list_odj_audio_inputs",
    "odj_audio_backend",
    "stop_odj_audio",
]
