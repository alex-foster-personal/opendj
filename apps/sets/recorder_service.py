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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from . import capture as capture_mod
from . import paths as sets_paths
from . import record as record_mod
from .master_mix import MASTER_MIX_DEVICE_LABEL, MasterMixRecordingStopped
from .state import SetsState

#: Manifest/DB capture_device for a session started without audio (SET-10).
NO_AUDIO_DEVICE_LABEL = "none (tracklist only)"
#: The last input REC started on, kept by the daemon under the sets root. Not
#: browser storage: the desktop shell serves the UI from a loopback port the OS
#: assigns per launch, and web storage is scoped to that port (SET-10).
REMEMBERED_INPUT_FILENAME = "recorder-input.json"

#: What a recording records (SET-12): ``master`` the app's own master bus,
#: streamed in by the page; ``loopback`` a loopback input (BlackHole);
#: ``external`` any other input (an audio interface carrying a hardware
#: mixer's output back in); ``none`` the tracklist only.
RecordSource = Literal["master", "loopback", "external", "none"]
RECORD_SOURCES: tuple[RecordSource, ...] = ("master", "loopback", "external", "none")
_DEVICE_SOURCES: frozenset[str] = frozenset({"loopback", "external"})

_log = logging.getLogger(__name__)


class RecorderConflict(RuntimeError):
    """Raised when a command conflicts with the owned recorder lifecycle."""


class RememberedInputUnreadable(RuntimeError):
    """Raised when the remembered REC input file exists but is not usable."""


class RecorderRequestInvalid(ValueError):
    """A start that names its source and its input inconsistently (SET-12)."""


class RecorderService:
    """Own one live recorder from start through stop or daemon shutdown."""

    def __init__(
        self,
        *,
        sets_root: Path | None = None,
        db_path: Path | None = None,
        capture_enabled: bool = True,
        list_devices: Callable[[], list[capture_mod.InputDevice]] = capture_mod.list_input_devices,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.sets_root = Path(sets_root or sets_paths.SETS_DIR)
        self.db_path = Path(db_path or sets_paths.SETS_DB)
        self.capture_enabled = capture_enabled
        self.list_devices = list_devices
        # The process environment the capture backend is chosen from
        # (``ODJ_AUDIO_BIN``); None reads this process's own.
        self.environ = environ
        self.remembered_input_path = self.sets_root / REMEMBERED_INPUT_FILENAME
        self._lock = threading.Lock()
        self._recorder: record_mod.Recorder | None = None
        self._source: RecordSource | None = None
        # Recordings this daemon stopped cleanly, newest last, so a chunk the
        # page had in flight at the stop is told apart from a lost one (SET-12).
        self._cleanly_stopped: list[str] = []

    def _idle_status(self) -> dict[str, Any]:
        return {
            "active": False,
            "session_id": None,
            "pid": None,
            "owned": False,
            "recoverable": False,
            "capture": "none",
            "capture_source": None,
            "recordings_dir": str(self.sets_root),
        }

    def _owned_status(self, recorder: record_mod.Recorder) -> dict[str, Any]:
        return {
            "active": True,
            "session_id": recorder.session_id,
            "pid": os.getpid(),
            "owned": True,
            "recoverable": False,
            "capture": recorder.capture_state(),
            "capture_error": recorder.capture_error(),
            "capture_source": self._source,
            "recordings_dir": str(self.sets_root),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._recorder is not None:
                return self._owned_status(self._recorder)
            external = record_mod.status(
                sets_root=self.sets_root,
                state=SetsState(db_path=self.db_path),
            )
        if not external["active"]:
            return self._idle_status()
        return {
            "active": True,
            "session_id": external.get("session_id"),
            "pid": external.get("pid"),
            "owned": False,
            "recoverable": not _pid_is_running(int(external["pid"])),
            # Another process owns that recording; its capture is not visible here.
            "capture": "unknown",
            "capture_source": None,
            "recordings_dir": str(self.sets_root),
        }

    def start(
        self,
        *,
        session_id: str | None,
        source: RecordSource,
        ffmpeg_device_idx: int | None = None,
        device_name: str | None = None,
        sources: tuple[str, ...],
    ) -> dict[str, Any]:
        """Start a recording of ``source``: the master mix, one input, or none.

        ``device_name`` is resolved to the backend's index HERE, at start: the
        index of a named input moves whenever another input is plugged in,
        so a remembered index records whatever now sits at it.
        """
        # Resolved BEFORE the lock: listing spawns ffmpeg (up to 10 s), and
        # status() shares the lock. A missing input or ffmpeg starts nothing.
        device_idx, device_label, backend = self._resolve_input(
            source, ffmpeg_device_idx, device_name
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
            master = source == "master"
            config = record_mod.RecorderConfig(
                sources=sources,
                capture_device_name=device_label,
                ffmpeg_device_idx=device_idx,
                capture_input_name=device_name,
                capture_backend=backend,
                master_mix=master,
                # The master mix needs no capture process, so a daemon run
                # without device capture (tests, CI) still records it.
                capture_disabled=source == "none" or not (self.capture_enabled or master),
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
            self._source = source
            if source in ("master", "none"):
                self._remember_input({"kind": source})
            elif device_name is not None:
                self._remember_input({"kind": "device", "name": device_name})
            return self._owned_status(recorder)

    def _resolve_input(
        self,
        source: RecordSource,
        ffmpeg_device_idx: int | None,
        device_name: str | None,
    ) -> tuple[int | None, str, capture_mod.CaptureBackend | None]:
        """(backend index or None, manifest label, the backend that will record
        it) for ``source``."""
        named = (ffmpeg_device_idx is not None) + (device_name is not None)
        if source not in RECORD_SOURCES:
            raise RecorderRequestInvalid(f"source {source!r} is not one of {RECORD_SOURCES}")
        if source in _DEVICE_SOURCES and named != 1:
            raise RecorderRequestInvalid(
                f"source {source!r} records one input: name exactly one of "
                "ffmpeg_device_idx or device_name"
            )
        if source not in _DEVICE_SOURCES and named != 0:
            raise RecorderRequestInvalid(f"source {source!r} records no input, so names none")
        if source == "none":
            return None, NO_AUDIO_DEVICE_LABEL, None
        if source == "master":
            return None, MASTER_MIX_DEVICE_LABEL, None
        backend = capture_mod.capture_backend(environ=self.environ) if self.capture_enabled else None
        if device_name is not None:
            return self._index_of(device_name, source), device_name, backend
        if backend is not None and backend.kind != "ffmpeg":
            # An ffmpeg index numbers AVFoundation's inputs; odj-audio lists
            # them in its own order, so the same number can be the room mic.
            raise capture_mod.CaptureUnavailable(
                f"ffmpeg_device_idx {ffmpeg_device_idx} numbers ffmpeg's inputs, but REC records "
                f"through {backend.kind} here; start it by device_name instead"
            )
        # A raw index is the scripts' escape hatch and is not listed, so its
        # loopback-or-not is taken as stated.
        return ffmpeg_device_idx, f"avfoundation input {ffmpeg_device_idx}", backend

    def remembered_input(self) -> dict[str, str] | None:
        """The source REC last started on, or none; None before any start.

        ``{"kind": "master"}``, ``{"kind": "none"}``, or ``{"kind": "device",
        "name": ...}``. Raises :class:`RememberedInputUnreadable` when the
        file exists but cannot be read or parsed, so a broken store is never
        shown as "nothing remembered yet".
        """
        try:
            parsed: Any = json.loads(self.remembered_input_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RememberedInputUnreadable(
                f"{self.remembered_input_path} could not be read: {exc}"
            ) from exc
        if parsed in ({"kind": "none"}, {"kind": "master"}):
            return dict(parsed)
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

    def _remember_input(self, choice: dict[str, str]) -> None:
        """Record the started source.

        Runs after the recording is live, so a failed write must not fail the
        start; it is logged as an error, and the stale choice it leaves is
        still a valid one the picker re-checks against connected inputs.
        """
        tmp = self.remembered_input_path.with_suffix(".json.tmp")
        try:
            self.remembered_input_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(choice), encoding="utf-8")
            os.replace(tmp, self.remembered_input_path)
        except OSError as exc:
            _log.error("could not remember the REC input in %s: %s", self.remembered_input_path, exc)

    def _index_of(self, device_name: str, source: RecordSource) -> int:
        devices = self.list_devices()
        matches = [device for device in devices if device.name == device_name]
        if len(matches) > 1:
            raise capture_mod.CaptureUnavailable(
                f"{len(matches)} audio inputs are named {device_name!r}; rename one in "
                "Audio MIDI Setup so REC can tell them apart"
            )
        if not matches:
            connected = ", ".join(repr(device.name) for device in devices) or "none"
            raise capture_mod.CaptureUnavailable(
                f"audio input {device_name!r} is not connected (connected inputs: {connected})"
            )
        if matches[0].loopback != (source == "loopback"):
            kind = "a loopback" if matches[0].loopback else "not a loopback"
            raise RecorderRequestInvalid(
                f"audio input {device_name!r} is {kind} input, so it cannot be source {source!r}"
            )
        return matches[0].index

    def mark_master_tap_attached(self, session_id: str) -> None:
        """Record when the page reported its tap connected (SET-12 timing)."""
        with self._lock:
            recorder = self._recorder
            if recorder is not None and recorder.session_id == session_id and recorder._master is not None:
                recorder._master.mark_tap_attached()

    def write_master_pcm(
        self, session_id: str, *, stream: str, seq: int, sample_rate: int, pcm: bytes
    ) -> None:
        """Append one master-mix chunk to the live recording (SET-12).

        Raises :class:`MasterMixRecordingStopped` for a chunk that was in
        flight when its recording was cleanly stopped (quiet, 410),
        :class:`RecorderConflict` when no owned master-mix recording
        with that id is live, and :class:`MasterMixChunkRefused` for a chunk
        that would leave a hole or is malformed. The file write happens
        outside the service lock so status reads never wait on the disk.
        """
        with self._lock:
            recorder = self._recorder
            if (recorder is None or recorder.session_id != session_id) and (
                session_id in self._cleanly_stopped
            ):
                raise MasterMixRecordingStopped(f"recording {session_id} was stopped")
            if recorder is None or recorder.session_id != session_id:
                active = "nothing" if recorder is None else f"session {recorder.session_id}"
                raise RecorderConflict(f"{active} is recording here, not {session_id}")
            writer = recorder._master
            if writer is None:
                raise RecorderConflict(
                    f"session {session_id} records {self._source!r}, not the master mix"
                )
        writer.append(stream=stream, seq=seq, sample_rate=sample_rate, pcm=pcm)

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
            if self._source == "master":
                self._cleanly_stopped = [*self._cleanly_stopped[-7:], self._recorder.session_id]
            self._recorder = None
            self._source = None
        return self._idle_status()

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
        return self._idle_status()

    def stop_owned_on_shutdown(self) -> None:
        """Settle any live HTTP-owned recorder before the daemon exits."""
        with self._lock:
            if self._recorder is None:
                return
            record_mod.stop(self._recorder)
            self._recorder = None
            self._source = None


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


__all__ = [
    "RECORD_SOURCES",
    "RecordSource",
    "RecorderConflict",
    "RecorderRequestInvalid",
    "RecorderService",
    "RememberedInputUnreadable",
]
