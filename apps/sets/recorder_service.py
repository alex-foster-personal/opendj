"""Server-owned lifecycle for the Sets recorder.

HTTP start/stop must retain the exact :class:`Recorder` instance because only
that object can settle its poll threads and ffmpeg process safely.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from . import paths as sets_paths
from . import record as record_mod
from .state import SetsState


class RecorderConflict(RuntimeError):
    """Raised when a command conflicts with the owned recorder lifecycle."""


class RecorderService:
    """Own one live recorder from start through stop or daemon shutdown."""

    def __init__(
        self,
        *,
        sets_root: Path | None = None,
        db_path: Path | None = None,
        capture_enabled: bool = True,
    ) -> None:
        self.sets_root = Path(sets_root or sets_paths.SETS_DIR)
        self.db_path = Path(db_path or sets_paths.SETS_DB)
        self.capture_enabled = capture_enabled
        self._lock = threading.Lock()
        self._recorder: record_mod.Recorder | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._recorder is not None:
                return {
                    "active": True,
                    "session_id": self._recorder.session_id,
                    "pid": os.getpid(),
                }
            external = record_mod.status(
                sets_root=self.sets_root,
                state=SetsState(db_path=self.db_path),
            )
        return {
            "active": bool(external["active"]),
            "session_id": external.get("session_id"),
            "pid": external.get("pid"),
        }

    def start(
        self,
        *,
        session_id: str | None,
        ffmpeg_device_idx: int,
        sources: tuple[str, ...],
    ) -> dict[str, Any]:
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
                ffmpeg_device_idx=ffmpeg_device_idx,
                capture_disabled=not self.capture_enabled,
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
            return {
                "active": True,
                "session_id": recorder.session_id,
                "pid": os.getpid(),
            }

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
        return {"active": False, "session_id": None, "pid": None}

    def stop_owned_on_shutdown(self) -> None:
        """Settle any live HTTP-owned recorder before the daemon exits."""
        with self._lock:
            if self._recorder is None:
                return
            record_mod.stop(self._recorder)
            self._recorder = None


__all__ = ["RecorderConflict", "RecorderService"]
