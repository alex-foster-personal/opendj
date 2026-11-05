"""The engine side of cloud offload: operations, one worker, the sweep timer.

OFFLOAD (spec ``(private path)`` section 8). Every UI action,
CLI verb and MCP tool becomes one :class:`Operation` on one worker thread, so
progress is honest (one transfer at a time, disk and network bound) and a
cancel lands between chunks. Operations are kept in memory for the progress
bar; durable state is the ledger, which is why an engine restart resumes the
tracks it left ``uploading`` or ``restoring`` (:meth:`OffloadService.resume`).

The same worker runs the re-offload sweep every
:data:`apps.cloud.offload.SWEEP_INTERVAL_S` when the daemon arms the service.

-Claude
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI

from apps.cloud import offload
from apps.cloud import offload_ledger as ledger
from apps.cloud.config import CloudConfig
from apps.cloud.lock import S3Client
from apps.cloud.offload_verify import OffloadCancelled, OffloadS3
from apps.shared.sync_runtime_gates import refuse_sync_round

from .coverage_recency import loaded_deck_ids
from .deps import data_dir_from_state_db

log = logging.getLogger(__name__)

OperationKind = Literal["offload", "restore", "sweep"]
OperationState = Literal["queued", "running", "done", "failed", "cancelled"]
THREAD_NAME = "opendj-cloud-offload"
FIRST_SWEEP_DELAY_S: float = 120.0
MAX_KEPT_OPERATIONS = 200

#: (cfg) -> (asset client, index CAS client). Tests inject in-memory stores.
ClientsFactory = Callable[[CloudConfig], tuple[OffloadS3, S3Client]]


def _production_clients(cfg: CloudConfig) -> tuple[OffloadS3, S3Client]:  # pragma: no cover - network
    from apps.cloud.offload_verify import boto3_offload_client
    from apps.cloud.replicate import boto3_s3_client

    return boto3_offload_client(cfg), boto3_s3_client(cfg)


@dataclass
class Operation:
    op_id: str
    kind: OperationKind
    stable_ids: list[str]
    mode: ledger.RestoreMode | None = None
    playlist_id: str | None = None
    state: OperationState = "queued"
    bytes_total: int = 0
    bytes_done: int = 0
    bytes_freed: int = 0
    tracks_done: int = 0
    current: str | None = None
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None
    report: dict[str, Any] | None = None
    dry_run: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)
    #: bytes_done when the current track started; per-track progress adds to it.
    track_base: int = 0

    def wire(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "kind": self.kind,
            "mode": self.mode,
            "playlist_id": self.playlist_id,
            "state": self.state,
            "stable_ids": list(self.stable_ids),
            "tracks_total": len(self.stable_ids),
            "tracks_done": self.tracks_done,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "bytes_freed": self.bytes_freed,
            "current": self.current,
            "results": {sid: dict(r) for sid, r in list(self.results.items())},
            "error": self.error,
            "report": self.report,
            "dry_run": self.dry_run,
        }


class OffloadService:
    """One worker thread draining a queue of operations, plus the sweep tick."""

    def __init__(self, app: FastAPI, *, clients: ClientsFactory = _production_clients) -> None:
        self._app = app
        self._clients = clients
        self._queue: queue.Queue[Operation | None] = queue.Queue()
        self._ops: dict[str, Operation] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sweep_timer: threading.Thread | None = None

    # --- context ---------------------------------------------------------

    @property
    def state_db(self) -> Path:
        return Path(getattr(self._app.state, "state_db_path", "(private path)"))

    @property
    def data_dir(self) -> Path:
        return data_dir_from_state_db(self.state_db)

    def _ui_mirror(self) -> Mapping[str, Any] | None:
        mirror = getattr(self._app.state, "ui_mirror", None)
        return mirror if isinstance(mirror, dict) else None

    def protected_ids(self) -> frozenset[str]:
        from apps.cloud import stem_hydration

        return stem_hydration.OPEN_DECKS.open_ids() | frozenset(loaded_deck_ids(self._ui_mirror()))

    def gate_reason(self) -> str | None:
        return refuse_sync_round(self.data_dir, self._ui_mirror())

    def _primary_path(self, stable_id: str) -> str | None:
        from apps.adapters.rekordbox.paths import _rekordbox_copy

        found = _rekordbox_copy(stable_id)
        return str(found) if isinstance(found, Path) else None

    def context(self, op: Operation) -> offload.OffloadContext:
        """Build the context; raises ``MissingEnvError`` naming absent R2 vars."""
        from apps.stems.artifacts import DEFAULT_STEMS_DIR

        cfg = CloudConfig.from_env()
        assets, index_s3 = self._clients(cfg)

        def on_progress(_sid: str, done: int, _total: int) -> None:
            op.bytes_done = op.track_base + done

        return offload.OffloadContext(
            data_dir=self.data_dir,
            state_db=self.state_db,
            stems_dir=Path(getattr(self._app.state, "stems_dir", DEFAULT_STEMS_DIR)),
            cfg=cfg,
            assets=assets,
            index_s3=index_s3,
            protected_ids=self.protected_ids,
            gate_reason=self.gate_reason,
            primary_path=self._primary_path,
            cancelled=op.cancel.is_set,
            on_progress=on_progress,
        )

    # --- operations --------------------------------------------------------

    def submit(
        self,
        kind: OperationKind,
        stable_ids: list[str],
        *,
        mode: ledger.RestoreMode | None = None,
        playlist_id: str | None = None,
        dry_run: bool = False,
    ) -> Operation:
        CloudConfig.from_env()  # fail the request, not the worker, on missing creds
        op = Operation(uuid.uuid4().hex[:12], kind, list(dict.fromkeys(stable_ids)), mode, playlist_id, dry_run=dry_run)
        with self._lock:
            self._ops[op.op_id] = op
            for stale in list(self._ops)[:-MAX_KEPT_OPERATIONS]:
                if self._ops[stale].state in ("done", "failed", "cancelled"):
                    del self._ops[stale]
        self._ensure_worker()
        self._queue.put(op)
        return op

    def get(self, op_id: str) -> Operation | None:
        with self._lock:
            return self._ops.get(op_id)

    def operations(self) -> list[Operation]:
        with self._lock:
            return list(self._ops.values())

    def cancel(self, op_id: str) -> Operation | None:
        op = self.get(op_id)
        if op is not None and op.state in ("queued", "running"):
            op.cancel.set()
        return op

    def _planned_bytes(self, ctx: offload.OffloadContext, op: Operation) -> int:
        if op.kind == "offload":
            total = 0
            for sid in op.stable_ids:
                total += sum(item.path.stat().st_size for item in offload.local_files(ctx, sid))
            return total
        with ledger.open_ledger(ctx.data_dir) as conn:
            return sum(
                f.size_bytes for sid in op.stable_ids for f in ledger.files_for(conn, sid) if f.deleted_at is not None
            )

    def run(self, op: Operation) -> None:
        """Run ``op`` to completion on the calling thread (the worker, or a test)."""
        op.state = "running"
        try:
            ctx = self.context(op)
            if op.kind == "sweep":
                op.report = offload.sweep(ctx, dry_run=op.dry_run).wire()
                op.state = "done"
                return
            op.bytes_total = self._planned_bytes(ctx, op)
            for sid in op.stable_ids:
                if op.cancel.is_set():
                    op.state = "cancelled"
                    return
                op.current = sid
                op.track_base = op.bytes_done
                try:
                    if op.kind == "offload":
                        outcome = offload.offload_track(ctx, sid)
                    else:
                        assert op.mode is not None
                        outcome = offload.restore_track(ctx, sid, op.mode)
                    op.bytes_freed += outcome.bytes_freed
                    op.results[sid] = {"state": outcome.state, "note": outcome.note, "error": None}
                except OffloadCancelled:
                    op.results[sid] = {"state": None, "note": None, "error": "cancelled"}
                    op.state = "cancelled"
                    return
                except offload.OffloadRefused as exc:
                    op.results[sid] = {"state": None, "note": None, "error": exc.code, "message": str(exc)}
                except Exception as exc:  # one track's failure never stops the batch
                    log.exception("cloud-offload: %s %s failed", op.kind, sid)
                    op.results[sid] = {"state": None, "note": None, "error": type(exc).__name__, "message": str(exc)}
                op.tracks_done += 1
            failed = [sid for sid, r in op.results.items() if r["error"]]
            op.state = "failed" if failed and len(failed) == len(op.stable_ids) else "done"
        except Exception as exc:
            log.exception("cloud-offload: operation %s failed", op.op_id)
            op.error = f"{type(exc).__name__}: {exc}"
            op.state = "failed"
        finally:
            op.current = None

    # --- threads -----------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._drain, name=THREAD_NAME, daemon=True)
            self._thread.start()

    def _drain(self) -> None:
        while not self._stop.is_set():
            op = self._queue.get()
            if op is None:
                return
            if op.cancel.is_set():
                op.state = "cancelled"
                continue
            self.run(op)

    def resume(self) -> list[Operation]:
        """Queue the tracks a restart left mid-operation."""
        ops: list[Operation] = []
        for row in offload.interrupted(self.data_dir):
            if row.state == "uploading":
                ops.append(self.submit("offload", [row.stable_id]))
            elif row.state == "restoring" and row.restore_mode is not None:
                ops.append(self.submit("restore", [row.stable_id], mode=row.restore_mode))
        return ops

    def arm(self, *, first_delay_s: float = FIRST_SWEEP_DELAY_S) -> None:
        """Daemon only: resume interrupted work, then sweep on the interval."""

        def tick_forever() -> None:
            if self._stop.wait(first_delay_s):
                return
            try:
                self.resume()
            except Exception:
                log.exception("cloud-offload: resume failed")
            while not self._stop.is_set():
                try:
                    self.submit("sweep", [])
                except Exception:
                    log.exception("cloud-offload: sweep not submitted")
                if self._stop.wait(offload.SWEEP_INTERVAL_S):
                    return

        self._sweep_timer = threading.Thread(target=tick_forever, name=f"{THREAD_NAME}-sweep", daemon=True)
        self._sweep_timer.start()

    def stop(self) -> None:
        self._stop.set()
        for op in self.operations():
            op.cancel.set()
        self._queue.put(None)
        for thread in (self._thread, self._sweep_timer):
            if thread is not None:
                thread.join(timeout=5.0)


def service_for(app: FastAPI) -> OffloadService:
    """The app's retained service, built on first use."""
    service: OffloadService | None = getattr(app.state, "cloud_offload_service", None)
    if service is None:
        factory = getattr(app.state, "cloud_offload_clients", None) or _production_clients
        service = OffloadService(app, clients=factory)
        app.state.cloud_offload_service = service
    return service


__all__ = ["OffloadService", "Operation", "service_for"]
