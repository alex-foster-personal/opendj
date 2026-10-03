"""Set recording orchestrator.

Plan 12-01 Step 5. Coordinates ffmpeg capture + deck-state pollers +
JSONL append + SQL mirroring + periodic heartbeats. The live lifecycle
is threads-based so tests can drive the loop deterministically; the
CLI wraps ``start`` / ``stop`` / ``resume`` / ``status`` / ``list``.

The recorder is intentionally testable without a real ffmpeg or
djay. Test fixtures pass ``capture_factory`` + ``source_factories``
callables that return stand-in objects.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.shared.paths import DJAY_WORKING_DB, REKORDBOX_WORKING_DB

from . import paths as sets_paths
from .capture import CAPTURE_STARTUP_CHECK_S, DEFAULT_DEVICE_NAME, CaptureHandle, start_capture
from .manifest import AudioSegment, Manifest, write_manifest
from .state import Event, SetsState

logger = logging.getLogger(__name__)

# Poll cadence for deck-state sources (CONTEXT D2).
DEFAULT_POLL_INTERVAL_S = 0.5
# Heartbeat cadence (Plan 12-01 Step 5).
DEFAULT_HEARTBEAT_INTERVAL_S = 60.0

_SESSION_ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:_\d+)?$")


# ---------------------------------------------------------------------------
# session id resolution
# ---------------------------------------------------------------------------


def _iso_session_id(now: datetime | None = None) -> str:
    dt = now or datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H-%M-%S")


def resolve_session_id(
    requested: str | None = None,
    *,
    root: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Return a collision-free session_id.

    If ``requested`` is given, validate + return as-is. Else build an
    ISO stamp and append ``_N`` if a dir already exists (CONTEXT open
    question on clock rollback).
    """
    base = Path(root) if root is not None else sets_paths.SETS_DIR
    if requested is not None:
        if not _SESSION_ID_PATTERN.match(requested):
            raise ValueError(
                f"invalid session_id {requested!r}; expected "
                "YYYY-MM-DDTHH-MM-SS[_N]"
            )
        return requested
    candidate = _iso_session_id(now)
    if not (base / candidate).exists():
        return candidate
    counter = 1
    while True:
        collide = f"{candidate}_{counter}"
        if not (base / collide).exists():
            return collide
        counter += 1


# ---------------------------------------------------------------------------
# JSONL sink
# ---------------------------------------------------------------------------


class TimelineJsonl:
    """Append-only JSONL writer for a session.

    Each event is one ``json.dumps`` line with ``\\n``. Writes flush so
    the file survives a crash mid-session.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, event: Event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), separators=(",", ":"), sort_keys=True)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")
            fh.flush()

    def iter_events(self) -> Iterator[dict[str, Any]]:
        """Yield every valid JSON line. Malformed trailing line is skipped."""
        if not self.path.exists():
            return iter(())
        def _gen() -> Iterator[dict[str, Any]]:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        return _gen()


# ---------------------------------------------------------------------------
# recorder
# ---------------------------------------------------------------------------


@dataclass
class RecorderConfig:
    """Knobs for the recorder. Sensible defaults match the plan spec."""

    sources: tuple[str, ...] = ("djay_monitor",)
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    capture_device_name: str = DEFAULT_DEVICE_NAME
    ffmpeg_device_idx: int | None = None
    # The input's exact name when REC picked it by name: odj-audio opens it
    # by that name rather than by an index that can move (SET-11).
    capture_input_name: str | None = None
    # When True we skip the ffmpeg subprocess entirely (test mode).
    capture_disabled: bool = False
    djay_db_path: Path | None = None
    rb_db_path: Path | None = None


@dataclass
class Recorder:
    """Live set recorder. One instance per active session.

    The object is thread-safe; the main thread drives ``start`` / ``stop``
    / ``run_poll_iteration`` (test hook). Source threads call
    ``state.record_event`` which is itself thread-safe.
    """

    session_id: str
    session_dir: Path
    config: RecorderConfig
    state: SetsState
    timeline: TimelineJsonl
    session_started_at: datetime
    _sources: dict[str, Any] = field(default_factory=dict)
    _capture: CaptureHandle | None = None
    _poll_threads: list[threading.Thread] = field(default_factory=list)
    _heartbeat_thread: threading.Thread | None = None
    _stop_event: threading.Event = field(default_factory=threading.Event)

    # ------------------------------------------------------------------
    # event helpers
    # ------------------------------------------------------------------

    def _rel_ts(self) -> float:
        delta = datetime.now(UTC) - self.session_started_at
        return max(0.0, delta.total_seconds())

    def _emit(self, event: Event) -> None:
        self.state.record_event(event)
        self.timeline.append(event)

    def _wall(self) -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds")

    def _emit_simple(self, action: str, value: dict[str, Any] | None = None) -> None:
        self._emit(
            Event(
                session_id=self.session_id,
                timestamp_s=self._rel_ts(),
                wall_clock=self._wall(),
                action=action,
                source="recorder",
                value=value or {},
            )
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start_capture(
        self,
        *,
        start_capture_fn: Callable[..., CaptureHandle] = start_capture,
    ) -> None:
        """Spawn ffmpeg unless the config disables capture."""
        if self.config.capture_disabled:
            return
        if self.config.ffmpeg_device_idx is None:
            logger.warning(
                "capture enabled but no ffmpeg_device_idx; skipping ffmpeg"
            )
            return
        self._capture = start_capture_fn(
            self.session_dir,
            self.config.ffmpeg_device_idx,
            startup_check_s=CAPTURE_STARTUP_CHECK_S,
            device_name=self.config.capture_input_name,
        )

    def capture_state(self) -> str:
        """``none`` without audio capture, else the capture's own state."""
        if self._capture is None:
            return "none"
        return self._capture.current_state()

    def attach_source(self, name: str, source_obj: Any) -> None:
        """Register a deck-state source that exposes ``poll_once()``."""
        if not hasattr(source_obj, "poll_once"):
            raise TypeError(f"source {name!r} must expose poll_once()")
        self._sources[name] = source_obj

    def run_poll_iteration(self) -> None:
        """Poll every registered source once (for deterministic tests)."""
        for name, source in self._sources.items():
            try:
                source.poll_once()
            except Exception as exc:  # pragma: no cover -- defensive
                logger.exception("source %s poll raised", name)
                self._emit_simple(
                    "source_error",
                    {"source": name, "kind": type(exc).__name__, "detail": str(exc)},
                )

    def _poll_loop(self, name: str, source: Any) -> None:
        while not self._stop_event.is_set():
            try:
                source.poll_once()
            except Exception as exc:  # pragma: no cover -- defensive
                logger.exception("source %s poll raised", name)
                self._emit_simple(
                    "source_error",
                    {"source": name, "kind": type(exc).__name__, "detail": str(exc)},
                )
            self._stop_event.wait(self.config.poll_interval_s)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            self._emit_simple("heartbeat")
            self._stop_event.wait(self.config.heartbeat_interval_s)

    def start_threads(self) -> None:
        """Kick off source-polling + heartbeat threads."""
        for name, source in self._sources.items():
            t = threading.Thread(
                target=self._poll_loop,
                args=(name, source),
                name=f"sets-source-{name}",
                daemon=True,
            )
            t.start()
            self._poll_threads.append(t)
        hb = threading.Thread(
            target=self._heartbeat_loop,
            name="sets-heartbeat",
            daemon=True,
        )
        hb.start()
        self._heartbeat_thread = hb

    def shutdown(
        self,
        *,
        stop_capture_fn: Callable[[CaptureHandle], int] | None = None,
    ) -> None:
        """Stop threads + ffmpeg; write the closing session_end event."""
        self._stop_event.set()
        for t in self._poll_threads:
            t.join(timeout=2.0)
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
        if self._capture is not None and stop_capture_fn is not None:
            try:
                stop_capture_fn(self._capture)
            except Exception:  # pragma: no cover -- guard
                logger.exception("ffmpeg stop raised")

    # ------------------------------------------------------------------
    # segments
    # ------------------------------------------------------------------

    def list_segments(self) -> list[AudioSegment]:
        """Return one :class:`AudioSegment` per audio segment on disk.

        Duration is cached (None) here; a follow-up step (Plan 12-03
        audio helper) can ffprobe if we need precise values. For the
        manifest it's enough to have the size + wall-clock start.
        """
        out: list[AudioSegment] = []
        for seg_path in sets_paths.segment_files(self.session_dir):
            stat = seg_path.stat()
            start_t = _segment_start_from_name(seg_path.name, self.session_started_at)
            out.append(
                AudioSegment(
                    name=seg_path.name,
                    start_t_s=start_t,
                    duration_s=None,
                    size_bytes=stat.st_size,
                )
            )
        return out


def _segment_start_from_name(
    name: str,
    session_started_at: datetime,
) -> float:
    """Extract the audio_<iso>.(wav|mp3) UTC timestamp and return rel seconds."""
    stem = Path(name).stem
    prefix = "audio_"
    if not stem.startswith(prefix):
        return 0.0
    raw = stem[len(prefix) :]
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H-%M-%S").replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    return max(0.0, (dt - session_started_at).total_seconds())


# ---------------------------------------------------------------------------
# high-level start/stop/resume/list
# ---------------------------------------------------------------------------


def _write_pid(session_dir: Path) -> Path:
    pid_path = session_dir / "recorder.pid"
    pid_path.write_text(str(os.getpid()))
    return pid_path


def _clear_pid(session_dir: Path) -> None:
    pid_path = session_dir / "recorder.pid"
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def _build_recorder(
    *,
    session_id: str,
    sets_root: Path,
    state: SetsState,
    config: RecorderConfig,
    started_at: datetime,
) -> Recorder:
    session_dir = sets_paths.session_dir(session_id, root=sets_root)
    session_dir.mkdir(parents=True, exist_ok=True)
    timeline = TimelineJsonl(session_dir / "timeline.jsonl")
    return Recorder(
        session_id=session_id,
        session_dir=session_dir,
        config=config,
        state=state,
        timeline=timeline,
        session_started_at=started_at,
    )


def start(
    *,
    session_id: str | None = None,
    config: RecorderConfig | None = None,
    sets_root: Path | None = None,
    state: SetsState | None = None,
    now: datetime | None = None,
    capture_factory: Callable[[Recorder], None] | None = None,
    source_factories: (
        dict[str, Callable[[Recorder], Any]] | None
    ) = None,
) -> Recorder:
    """Bring up a new recorder; NOT launched into a daemon.

    The returned :class:`Recorder` is ready for ``start_threads`` (live
    mode) or ``run_poll_iteration`` (test mode). CLI callers use
    ``start_threads`` + signal handlers.

    ``capture_factory`` + ``source_factories`` let tests inject mock
    sources without monkeypatching. Default factories spin up the real
    ``DjaySource`` / ``RekordboxHistorySource`` against the working
    copies of djay / rekordbox DBs.
    """
    cfg = config or RecorderConfig()
    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    started = now or datetime.now(UTC)
    sid = resolve_session_id(session_id, root=root, now=started)
    state_obj = state or SetsState()

    # Create DB row + dir BEFORE any side-effecty factories.
    state_obj.open_session(
        sid,
        capture_device=cfg.capture_device_name,
        started_at=started.isoformat(timespec="milliseconds"),
    )
    recorder = _build_recorder(
        session_id=sid,
        sets_root=root,
        state=state_obj,
        config=cfg,
        started_at=started,
    )
    _write_pid(recorder.session_dir)
    recorder._emit_simple(
        "session_start",
        {
            "capture_device": cfg.capture_device_name,
            "sources": list(cfg.sources),
        },
    )

    # Capture (ffmpeg).
    if capture_factory is not None:
        capture_factory(recorder)
    elif not cfg.capture_disabled:
        recorder.start_capture()

    # Sources.
    factories = source_factories or _default_source_factories(cfg)
    for name in cfg.sources:
        factory = factories.get(name)
        if factory is None:
            logger.warning("unknown source %r; skipping", name)
            continue
        try:
            obj = factory(recorder)
        except Exception as exc:  # pragma: no cover -- guard
            logger.exception("source factory %s failed", name)
            recorder._emit_simple(
                "source_error",
                {"source": name, "kind": type(exc).__name__, "detail": str(exc)},
            )
            continue
        recorder.attach_source(name, obj)

    return recorder


def stop(
    recorder: Recorder,
    *,
    stop_capture_fn: Callable[[CaptureHandle], int] | None = None,
    ended_at: datetime | None = None,
) -> Manifest:
    """Clean-stop the recorder; write manifest.json; update sets.ended_at."""
    from .capture import stop_capture  # late import to keep module light

    end_at = ended_at or datetime.now(UTC)
    recorder._emit_simple("session_end")
    recorder.shutdown(stop_capture_fn=stop_capture_fn or stop_capture)
    recorder.state.end_session(
        recorder.session_id,
        ended_at=end_at.isoformat(timespec="milliseconds"),
    )
    _clear_pid(recorder.session_dir)
    session_row = recorder.state.get_session(recorder.session_id)
    manifest = Manifest(
        session_id=recorder.session_id,
        started_at=(
            session_row.started_at
            if session_row is not None
            else recorder.session_started_at.isoformat(timespec="milliseconds")
        ),
        ended_at=end_at.isoformat(timespec="milliseconds"),
        capture_device=recorder.config.capture_device_name,
        share_state=session_row.share_state if session_row is not None else "private",
        event_count=recorder.state.count_events(recorder.session_id),
        deck_sources=list(recorder.config.sources),
        mp3_segments=recorder.list_segments(),
    )
    write_manifest(recorder.session_dir, manifest)
    return manifest


def finalize(
    session_id: str,
    *,
    sets_root: Path | None = None,
    state: SetsState | None = None,
    ended_at: datetime | None = None,
) -> Manifest:
    """Finalise a session without a live :class:`Recorder`.

    This is the crash-recovery / CLI-``stop`` path: the recorder process
    may have died or may live in another process. We rebuild just enough
    state to emit ``session_end`` on the timeline, mark the sets row
    ended, clear the pid, and write ``manifest.json``. No ffmpeg / no
    source threads are touched — those belong to the owning process.

    Codex Phase 12 review finding: the prior CLI-stop only marked the
    DB row ended and unlinked the pid, leaving ``manifest.json`` absent
    and the recording in a broken state for downstream tools.
    """
    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    state_obj = state or SetsState()
    row = state_obj.get_session(session_id)
    if row is None:
        raise KeyError(f"no set session {session_id!r}")
    started = datetime.fromisoformat(row.started_at)
    session_dir = sets_paths.session_dir(session_id, root=root)
    session_dir.mkdir(parents=True, exist_ok=True)
    timeline = TimelineJsonl(session_dir / "timeline.jsonl")
    # Minimal recorder view so list_segments() / _emit_simple() work.
    recorder = Recorder(
        session_id=session_id,
        session_dir=session_dir,
        config=RecorderConfig(
            capture_device_name=row.capture_device or DEFAULT_DEVICE_NAME
        ),
        state=state_obj,
        timeline=timeline,
        session_started_at=started,
    )
    end_at = ended_at or datetime.now(UTC)
    if row.ended_at is None:
        recorder._emit_simple("session_end")
    state_obj.end_session(
        session_id,
        ended_at=end_at.isoformat(timespec="milliseconds"),
    )
    _clear_pid(session_dir)
    session_row = state_obj.get_session(session_id) or row
    manifest = Manifest(
        session_id=session_id,
        started_at=session_row.started_at,
        ended_at=session_row.ended_at or end_at.isoformat(timespec="milliseconds"),
        capture_device=session_row.capture_device or DEFAULT_DEVICE_NAME,
        share_state=getattr(session_row, "share_state", "private") or "private",
        event_count=state_obj.count_events(session_id),
        deck_sources=[],
        mp3_segments=recorder.list_segments(),
    )
    write_manifest(session_dir, manifest)
    return manifest


def resume(
    session_id: str,
    *,
    config: RecorderConfig | None = None,
    sets_root: Path | None = None,
    state: SetsState | None = None,
    _now: datetime | None = None,
    capture_factory: Callable[[Recorder], None] | None = None,
    source_factories: dict[str, Callable[[Recorder], Any]] | None = None,
) -> Recorder:
    """Re-open an unfinalised session; emits ``session_resume``.

    Reads the existing sets row for ``started_at`` (our anchor for
    timestamp_s) so subsequent events line up with the existing
    timeline.jsonl.
    """
    cfg = config or RecorderConfig()
    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    state_obj = state or SetsState()
    row = state_obj.get_session(session_id)
    if row is None:
        raise KeyError(f"no set session {session_id!r}")
    started = datetime.fromisoformat(row.started_at)
    recorder = _build_recorder(
        session_id=session_id,
        sets_root=root,
        state=state_obj,
        config=cfg,
        started_at=started,
    )
    _write_pid(recorder.session_dir)
    recorder._emit_simple(
        "session_resume",
        {"sources": list(cfg.sources)},
    )
    if capture_factory is not None:
        capture_factory(recorder)
    elif not cfg.capture_disabled:
        recorder.start_capture()
    factories = source_factories or _default_source_factories(cfg)
    for name in cfg.sources:
        factory = factories.get(name)
        if factory is None:
            continue
        try:
            obj = factory(recorder)
        except Exception as exc:  # pragma: no cover -- guard
            recorder._emit_simple(
                "source_error",
                {"source": name, "kind": type(exc).__name__, "detail": str(exc)},
            )
            continue
        recorder.attach_source(name, obj)
    return recorder


def _default_source_factories(
    cfg: RecorderConfig,
) -> dict[str, Callable[[Recorder], Any]]:
    """Return factories that build the live deck-state sources."""
    from .sources.djay_source import DjaySource
    from .sources.opendj_source import OpenDjDeckSource
    from .sources.rb_source import RekordboxHistorySource

    def make_djay(rec: Recorder) -> Any:
        return DjaySource(
            session_id=rec.session_id,
            state=rec.state,
            db_path=cfg.djay_db_path or DJAY_WORKING_DB,
            session_started_at=rec.session_started_at,
        )

    def make_rb(rec: Recorder) -> Any:
        return RekordboxHistorySource(
            session_id=rec.session_id,
            state=rec.state,
            db_path=cfg.rb_db_path or REKORDBOX_WORKING_DB,
            session_started_at=rec.session_started_at,
        )

    def make_opendj(rec: Recorder) -> Any:
        # Push-fed: the HTTP ingest calls submit(), this poll drains it.
        return OpenDjDeckSource(
            session_id=rec.session_id,
            state=rec.state,
            session_started_at=rec.session_started_at,
        )

    return {
        "djay_monitor": make_djay,
        "rb_history": make_rb,
        "opendj_decks": make_opendj,
    }


def status(
    *,
    sets_root: Path | None = None,
    state: SetsState | None = None,  # noqa: ARG001 - see below
) -> dict[str, Any]:
    """Return a summary of any active session (pid file present).

    ``state`` is unread here: the answer comes from the pid file on disk, not
    from the sets DB. It stays in the signature under its public name because
    every caller in ``apps/sets/recorder_service.py`` passes it by keyword.
    Commit 17c7e99da renamed it to ``_state`` to satisfy ARG001, which turned
    ``GET /api/v1/sets/recorder/status`` into a 500 on every request -- and
    since the performance page polls that route, it failed every e2e browser
    test in the repo.
    """
    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    if not root.exists():
        return {"active": False}
    for session_dir in sorted(root.iterdir()):
        if not session_dir.is_dir():
            continue
        pid_path = session_dir / "recorder.pid"
        if not pid_path.exists():
            continue
        try:
            pid = int(pid_path.read_text().strip())
        except ValueError:
            continue
        return {
            "active": True,
            "session_id": session_dir.name,
            "pid": pid,
        }
    return {"active": False}


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "resolve_session_id",
    "TimelineJsonl",
    "RecorderConfig",
    "Recorder",
    "start",
    "stop",
    "finalize",
    "resume",
    "status",
]
