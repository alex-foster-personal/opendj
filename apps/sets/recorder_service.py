"""Server-owned lifecycle for the Sets recorder.

HTTP start/stop must retain the exact :class:`Recorder` instance because only
that object can settle its poll threads and ffmpeg process safely.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import capture as capture_mod
from . import paths as sets_paths
from . import record as record_mod
from .state import SetsState

#: Manifest/DB capture_device for a session started without audio (SET-10).
NO_AUDIO_DEVICE_LABEL = "none (tracklist only)"
#: The last input REC started on, kept by the daemon under the sets root. Not
#: browser storage: the desktop shell serves the UI from a loopback port the OS
#: assigns per launch, and web storage is scoped to that port (SET-10).
REMEMBERED_INPUT_FILENAME = "recorder-input.json"

_log = logging.getLogger(__name__)


class RecorderConflict(RuntimeError):
    """Raised when a command conflicts with the owned recorder lifecycle."""


class RememberedInputUnreadable(RuntimeError):
    """Raised when the remembered REC input file exists but is not usable."""


class RecorderService:
    """Own one live recorder from start through stop or daemon shutdown."""

    def __init__(
        self,
        *,
        sets_root: Path | None = None,
        db_path: Path | None = None,
        capture_enabled: bool = True,
        list_devices: Callable[[], list[capture_mod.InputDevice]] = capture_mod.list_input_devices,
    ) -> None:
        self.sets_root = Path(sets_root or sets_paths.SETS_DIR)
        self.db_path = Path(db_path or sets_paths.SETS_DB)
        self.capture_enabled = capture_enabled
        self.list_devices = list_devices
        self.remembered_input_path = self.sets_root / REMEMBERED_INPUT_FILENAME
        self._lock = threading.Lock()
        self._recorder: record_mod.Recorder | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._recorder is not None:
                return {
                    "active": True,
                    "session_id": self._recorder.session_id,
                    "pid": os.getpid(),
                    "owned": True,
                    "recoverable": False,
                    "capture": self._recorder.capture_state(),
                }
            external = record_mod.status(
                sets_root=self.sets_root,
                state=SetsState(db_path=self.db_path),
            )
        return {
            "active": bool(external["active"]),
            "session_id": external.get("session_id"),
            "pid": external.get("pid"),
            "owned": False,
            "recoverable": bool(
                external["active"] and not _pid_is_running(int(external["pid"]))
            ),
            # Another process owns that recording; its capture is not visible here.
            "capture": "unknown" if external["active"] else "none",
        }

    def start(
        self,
        *,
        session_id: str | None,
        ffmpeg_device_idx: int | None = None,
        device_name: str | None = None,
        capture_audio: bool = True,
        sources: tuple[str, ...],
    ) -> dict[str, Any]:
        """Start a recording on one audio input, or on none.

        ``device_name`` is resolved to ffmpeg's index HERE, at start: the
        index of a named input moves whenever another input is plugged in,
        so a remembered index records whatever now sits at it.
        """
        # Resolved BEFORE the lock: listing spawns ffmpeg (up to 10 s), and
        # status() shares the lock. A missing input or ffmpeg starts nothing.
        device_idx, device_label = self._resolve_input(
            ffmpeg_device_idx, device_name, capture_audio=capture_audio
        )
        with self._lock:
            if self._recorder is not None:
                raise RecorderConflict(
                    f"session {self._recorder.session_id} is already recording"
                )
            external = record_mod.status(
                sets_root=self.sets_root,
                state=SetsState(db_path=self.db_path),
            )
            if external["active"]:
                raise RecorderConflict(
                    f"session {external['session_id']} is owned by recorder "
                    f"process {external['pid']}"
                )

            state = SetsState(db_path=self.db_path)
            resolved_id = record_mod.resolve_session_id(
                session_id,
                root=self.sets_root,
            )
            config = record_mod.RecorderConfig(
                sources=sources,
                capture_device_name=device_label,
                ffmpeg_device_idx=device_idx,
                capture_input_name=device_name,
                capture_disabled=not (self.capture_enabled and capture_audio),
            )
            recorder: record_mod.Recorder | None = None
            try:
                recorder = record_mod.start(
                    session_id=resolved_id,
                    config=config,
                    sets_root=self.sets_root,
                    state=state,
                )
                recorder.start_threads()
            except Exception:
                if recorder is not None:
                    record_mod.stop(recorder)
                elif state.get_session(resolved_id):
                    record_mod.finalize(
                        resolved_id,
                        sets_root=self.sets_root,
                        state=state,
                    )
                raise
            self._recorder = recorder
            if device_name is not None or not capture_audio:
                self._remember_input(device_name if capture_audio else None)
            return {
                "active": True,
                "session_id": recorder.session_id,
                "pid": os.getpid(),
                "owned": True,
                "recoverable": False,
                "capture": recorder.capture_state(),
            }

    def _resolve_input(
        self,
        ffmpeg_device_idx: int | None,
        device_name: str | None,
        *,
        capture_audio: bool,
    ) -> tuple[int | None, str]:
        """(ffmpeg index or None, manifest label) for one input, or for none."""
        if capture_audio == (ffmpeg_device_idx is None and device_name is None):
            raise ValueError(
                "name exactly one audio input (ffmpeg_device_idx or device_name), "
                "or set capture_audio false for a tracklist-only recording"
            )
        if ffmpeg_device_idx is not None and device_name is not None:
            raise ValueError("ffmpeg_device_idx and device_name are mutually exclusive")
        if not capture_audio:
            return None, NO_AUDIO_DEVICE_LABEL
        if self.capture_enabled:
            capture_mod.capture_backend()
        if device_name is not None:
            return self._index_of(device_name), device_name
        return ffmpeg_device_idx, f"avfoundation input {ffmpeg_device_idx}"

    def remembered_input(self) -> dict[str, str] | None:
        """The input REC last started on by name, or none; None before any start.

        Raises :class:`RememberedInputUnreadable` when the file exists but
        cannot be read or parsed, so a broken store is never shown as
        "nothing remembered yet".
        """
        try:
            parsed: Any = json.loads(self.remembered_input_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RememberedInputUnreadable(
                f"{self.remembered_input_path} could not be read: {exc}"
            ) from exc
        if parsed == {"kind": "none"}:
            return {"kind": "none"}
        if (
            isinstance(parsed, dict)
            and parsed.keys() == {"kind", "name"}
            and parsed["kind"] == "device"
            and isinstance(parsed["name"], str)
            and parsed["name"]
        ):
            return {"kind": "device", "name": parsed["name"]}
        raise RememberedInputUnreadable(
            f"{self.remembered_input_path} does not hold an input choice: {parsed!r}"
        )

    def _remember_input(self, device_name: str | None) -> None:
        """Record the started input.

        Runs after the recording is live, so a failed write must not fail the
        start; it is logged as an error, and the stale choice it leaves is
        still a valid one the picker re-checks against connected inputs.
        """
        choice = {"kind": "none"} if device_name is None else {"kind": "device", "name": device_name}
        tmp = self.remembered_input_path.with_suffix(".json.tmp")
        try:
            self.remembered_input_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(choice), encoding="utf-8")
            os.replace(tmp, self.remembered_input_path)
        except OSError as exc:
            _log.error("could not remember the REC input in %s: %s", self.remembered_input_path, exc)

    def _index_of(self, device_name: str) -> int:
        devices = self.list_devices()
        matches = [device.index for device in devices if device.name == device_name]
        if len(matches) > 1:
            raise capture_mod.CaptureUnavailable(
                f"{len(matches)} audio inputs are named {device_name!r}; rename one in "
                "Audio MIDI Setup so REC can tell them apart"
            )
        if matches:
            return matches[0]
        connected = ", ".join(repr(device.name) for device in devices) or "none"
        raise capture_mod.CaptureUnavailable(
            f"audio input {device_name!r} is not connected (connected inputs: {connected})"
        )

    def active_source(self, name: str) -> Any:
        """Return the named source attached to the owned recorder.

        Raises :class:`RecorderConflict` when nothing is recording, or
        when the live session did not enable this source. Both are the
        caller's problem to fix, so neither is papered over with a
        silently-discarded no-op.
        """
        with self._lock:
            if self._recorder is None:
                raise RecorderConflict(
                    f"no HTTP-owned recorder is active; start one with "
                    f"sources including {name!r} before sending observations"
                )
            source = self._recorder._sources.get(name)
            if source is None:
                raise RecorderConflict(
                    f"session {self._recorder.session_id} is recording without "
                    f"the {name!r} source; its enabled sources are "
                    f"{sorted(self._recorder._sources)}"
                )
            return source

    def stop(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            if self._recorder is None:
                external = record_mod.status(
                    sets_root=self.sets_root,
                    state=SetsState(db_path=self.db_path),
                )
                if external["active"]:
                    raise RecorderConflict(
                        f"session {external['session_id']} belongs to recorder "
                        f"process {external['pid']}; stop it through its owning daemon"
                    )
                raise RecorderConflict("no HTTP-owned recorder is active")
            if self._recorder.session_id != session_id:
                raise RecorderConflict(
                    f"session {self._recorder.session_id} is active, not {session_id}"
                )
            record_mod.stop(self._recorder)
            self._recorder = None
        return {
            "active": False,
            "session_id": None,
            "pid": None,
            "owned": False,
            "recoverable": False,
            "capture": "none",
        }

    def recover_stale(self, session_id: str, expected_pid: int) -> dict[str, Any]:
        """Finalize a crashed recorder only after proving its PID is not live."""
        with self._lock:
            if self._recorder is not None:
                raise RecorderConflict("the HTTP-owned recorder is still active")
            external = record_mod.status(
                sets_root=self.sets_root,
                state=SetsState(db_path=self.db_path),
            )
            if not external["active"]:
                raise RecorderConflict("no stale recorder is present")
            if external["session_id"] != session_id or external["pid"] != expected_pid:
                raise RecorderConflict("recorder ownership changed; refresh before recovery")
            if _pid_is_running(expected_pid):
                raise RecorderConflict(
                    f"recorder process {expected_pid} is still running"
                )
            record_mod.finalize(
                session_id,
                sets_root=self.sets_root,
                state=SetsState(db_path=self.db_path),
            )
        return {
            "active": False,
            "session_id": None,
            "pid": None,
            "owned": False,
            "recoverable": False,
            "capture": "none",
        }

    def stop_owned_on_shutdown(self) -> None:
        """Settle any live HTTP-owned recorder before the daemon exits."""
        with self._lock:
            if self._recorder is None:
                return
            record_mod.stop(self._recorder)
            self._recorder = None


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


__all__ = ["RecorderConflict", "RecorderService", "RememberedInputUnreadable"]
