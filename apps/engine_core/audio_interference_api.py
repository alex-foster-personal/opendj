"""``GET /api/v1/audio-interference`` -- software likely to fight for the mic.

The browser cannot see running processes, so cue alignment has no way to tell
an operator that another app is holding the microphone. The engine can, and
this is the one place that says so.

`supported: false` is NOT the same as "nothing found" and is never rendered as
a pass: a probe that could not run has to say it could not run.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

from apps.shared.audio_interference import detect_audio_interference

AUDIO_INTERFERENCE_PATH: str = "/api/v1/audio-interference"

#: Where macOS loads third-party CoreAudio plugins from. A plugin here is
#: loaded by coreaudiod whether or not its application is running.
HAL_PLUGIN_DIRS: tuple[str, ...] = (
    "/Library/Audio/Plug-Ins/HAL",
    "~/Library/Audio/Plug-Ins/HAL",
)


class AudioInterferenceItemOut(BaseModel):
    key: str
    label: str
    kind: str
    why: str
    matched: str


class AudioInterferenceOut(BaseModel):
    supported: bool
    detected: list[AudioInterferenceItemOut]
    error: str | None = None


def read_running_processes() -> tuple[str, ...]:
    """Every running process's executable path, best effort per process.

    A process that vanishes or refuses inspection between listing and reading
    is skipped rather than failing the whole scan; a scan that cannot start at
    all raises, and the route turns that into `supported: false`.
    """
    import psutil

    names: list[str] = []
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            info = proc.info
            names.append(str(info.get("exe") or info.get("name") or ""))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return tuple(name for name in names if name != "")


def read_audio_drivers() -> tuple[str, ...]:
    found: list[str] = []
    for raw in HAL_PLUGIN_DIRS:
        directory = Path(os.path.expanduser(raw))
        if not directory.is_dir():
            continue
        found.extend(entry.name for entry in directory.iterdir())
    return tuple(found)


def add_audio_interference_route(
    app: FastAPI,
    *,
    read_processes: Callable[[], Sequence[str]] = read_running_processes,
    read_audio_drivers: Callable[[], Sequence[str]] = read_audio_drivers,
) -> None:
    @app.get(
        AUDIO_INTERFERENCE_PATH,
        response_model=AudioInterferenceOut,
        tags=["health"],
        name="audio_interference",
    )
    def audio_interference() -> AudioInterferenceOut:
        try:
            processes = read_processes()
            drivers = read_audio_drivers()
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            return AudioInterferenceOut(supported=False, detected=[], error=str(exc))
        found = detect_audio_interference(process_names=processes, driver_names=drivers)
        return AudioInterferenceOut(
            supported=True,
            detected=[
                AudioInterferenceItemOut(
                    key=item.key,
                    label=item.label,
                    kind=item.kind.value,
                    why=item.why,
                    matched=item.matched,
                )
                for item in found
            ],
            error=None,
        )


__all__ = [
    "AUDIO_INTERFERENCE_PATH",
    "AudioInterferenceItemOut",
    "AudioInterferenceOut",
    "add_audio_interference_route",
    "read_audio_drivers",
    "read_running_processes",
]
