"""CLI driver for the backfill queue: ``python -m apps.analysis.queue_cli``.

Agent-native parity (repo house rule, and spec section 4's own line "Every UI
control (toggle, queue, re-analyze) has a CLI and an HTTP endpoint"): every
queue control the UI panel offers exists here and at
``/api/v1/analysis/backfill/*``. The three surfaces call ONE implementation,
:mod:`apps.analysis.queue`, so they cannot drift into three behaviors.

Subcommands
-----------

``enqueue``       Plan a batch from stable_ids: admission rule, worker count,
                  band, and one row per refusal with its named reason.
``progress``      Aggregate + per-item state for a batch.
``cancel``        Cancel pending AND in-flight items.
``resume``        Put a cancelled or abandoned batch back in the queue.
``run``           Drain a batch across the worker pool the plan sized.
``version-bump``  Re-queue every track whose record for a producer predates
                  a version.
``list``          Recent batches.

Every subcommand prints JSON on stdout with ``--json`` so an agent can drive
it without parsing a table.

Exit codes
----------

``0``  The command did what it says.
``1``  ``EXIT_TRACK_FAILURES`` -- a drain finished with at least one track
       whose backend failed on it. Says nothing about the next batch.
``2``  ``EXIT_USAGE`` -- bad flags, unknown backend, unknown batch.
``5``  ``EXIT_INTERNAL_ERROR`` -- an exception escaped, so no verdict was
       reached at all. Distinct from 1 because Python exits 1 for an
       escaping exception, which would otherwise read as "some tracks
       failed".

-Claude
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from . import queue as queue_api
from . import queue_store, queue_targets, queue_user_cli
from .backends import get_backend
from .backends.base import AnalyzerBackend
from .lanes import own_backend, parse_own_backend
from .queue_runner import drain, run_batch
from .store import open_conn

log = logging.getLogger("apps.analysis.queue_cli")
console = Console()

EXIT_OK: int = 0
EXIT_TRACK_FAILURES: int = 1
EXIT_USAGE: int = 2
EXIT_INTERNAL_ERROR: int = 5


def resolve_backend(spec: str) -> type[AnalyzerBackend]:
    """A registry name, or an explicit ``module:ClassName`` import path.

    The import-path form is not a convenience: a wave-1 producer lane ships
    its analyzer as a package that registers itself on import, and a test
    drives a probe backend that is deliberately not in the shipped registry.
    Both need to be namable from the command line without the registry
    growing a special case, and both are EXPLICIT at the call site, which a
    silent "try the registry, then guess an import" fall-back would not be.
    """
    if ":" not in spec:
        return get_backend(spec)
    module_name, _, class_name = spec.partition(":")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, class_name)
    except AttributeError as exc:
        raise KeyError(
            f"module {module_name!r} has no attribute {class_name!r}"
        ) from exc


def backend_for_lane(run_backend: type[AnalyzerBackend], lane: str) -> type[AnalyzerBackend]:
    """The producer a ``run`` uses for ``lane``: its own backend for its own
    lane, else that lane's backfill producer.

    A cascade batch is for the DEPENDENT lane (beatgrid moved, key re-queues).
    Handing it the run's beatgrid backend enqueued key items under
    ``own_beatgrid.backfill``, which the runner then skipped as
    ``already_current`` because the beatgrid record it checked was the one
    that had just been written: 348 key items on demon-llama, Thu 1 Oct 2026,
    and not one key computed.
    """
    parsed = parse_own_backend(run_backend.name)
    if parsed is None or parsed.lane == lane:
        return run_backend
    return get_backend(own_backend(lane, "backfill"))


def _conn(db: str | None) -> sqlite3.Connection:
    conn = open_conn(Path(db) if db else None)
    queue_store.ensure_queue_tables(conn)
    return conn


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))


#-----------------------------------------------------------------------------
# subcommands
#-----------------------------------------------------------------------------

def cmd_enqueue(args: argparse.Namespace) -> int:
    conn = _conn(args.db)
    try:
        candidates = queue_targets.candidates_from_state(
            conn, args.stable_id, lane=args.lane, backend=args.backend
        )
        result = queue_api.enqueue(conn, candidates, note=args.note)
    finally:
        conn.close()
    payload = {
        "batch_id": result.batch_id,
        "offered": result.offered,
        "admitted": result.admitted,
        "refused": result.refused,
        "workers": result.workers,
        "band": result.band,
        "memory_model": {
            "backend": result.model.backend,
            "producer_version": result.model.producer_version,
            "floor_mb": result.model.floor_mb,
            "slope_mb_per_min": result.model.slope_mb_per_min,
            "measured_on": result.model.measured_on,
            "source": result.model.source,
        },
    }
    if not args.json:
        console.print(
            f"[bold]{result.batch_id}[/bold]: offered {result.offered}, "
            f"admitted {result.admitted}, refused {result.refused}; "
            f"{result.workers} worker(s), band {result.band}"
        )
    _emit(payload, as_json=args.json)
    return EXIT_OK


def cmd_progress(args: argparse.Namespace) -> int:
    conn = _conn(args.db)
    try:
        prog = queue_api.progress(conn, args.batch_id, item_limit=args.limit)
    finally:
        conn.close()
    payload = {
        "batch_id": prog.batch_id,
        "state": prog.state,
        "workers": prog.workers,
        "band": prog.band,
        "memory_model": prog.memory_model,
        "counts": prog.counts,
        "total": prog.total,
        "settled": prog.settled,
        "created_at": prog.created_at,
        "updated_at": prog.updated_at,
        "items": [
            {
                "stable_id": i.stable_id,
                "lane": i.lane,
                "backend": i.backend,
                "state": i.state,
                "reason": i.reason,
                "attempts": i.attempts,
                "duration_s": i.duration_s,
                "predicted_peak_mb": i.predicted_peak_mb,
            }
            for i in prog.items
        ],
    }
    if not args.json:
        table = Table(title=f"{prog.batch_id} ({prog.state})")
        table.add_column("state")
        table.add_column("count", justify="right")
        for state, count in prog.counts.items():
            table.add_row(state, str(count))
        console.print(table)
        console.print(
            f"{prog.settled}/{prog.total} settled; {prog.workers} worker(s), "
            f"band {prog.band}"
        )
    _emit(payload, as_json=args.json)
    return EXIT_OK


def cmd_cancel(args: argparse.Namespace) -> int:
    conn = _conn(args.db)
    try:
        cancelled = queue_api.cancel(conn, args.batch_id)
    finally:
        conn.close()
    if not args.json:
        console.print(f"cancelled {cancelled} open item(s) in {args.batch_id}")
    _emit({"batch_id": args.batch_id, "cancelled": cancelled}, as_json=args.json)
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    conn = _conn(args.db)
    try:
        revived = queue_api.resume(conn, args.batch_id)
        prog = queue_api.progress(conn, args.batch_id, item_limit=0)
    finally:
        conn.close()
    if not args.json:
        console.print(
            f"{args.batch_id}: {revived} item(s) back in the queue, "
            f"{prog.workers} worker(s), band {prog.band}"
        )
    _emit(
        {
            "batch_id": args.batch_id,
            "requeued": revived,
            "workers": prog.workers,
            "band": prog.band,
            "counts": prog.counts,
        },
        as_json=args.json,
    )
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    backend_cls = resolve_backend(args.backend)
    conn = _conn(args.db)
    try:
        def cascade_resolver(
            conn_: sqlite3.Connection,
            stable_ids: list[str],
            *,
            lane: str,
            backend: str | None = None,
        ) -> list:
            return queue_targets.candidates_from_state(
                conn_, stable_ids, lane=lane,
                backend=backend or backend_for_lane(backend_cls, lane).name,
            )

        summaries = drain(
            conn,
            args.batch_id,
            backend_for_lane=lambda lane: backend_for_lane(backend_cls, lane),
            cascade_resolver=cascade_resolver,
        )
        summary = summaries[-1] if summaries else None
        if summary is None:
            items = queue_store.list_items(conn, args.batch_id, limit=1)
            if not items:
                raise queue_api.QueueError(f"batch {args.batch_id!r} has no items")
            summary = run_batch(
                conn,
                args.batch_id,
                backend_cls=backend_cls,
                cascade_resolver=cascade_resolver,
            )
    finally:
        conn.close()
    payload = {
        "batch_id": summary.batch_id,
        "runner_id": summary.runner_id,
        "workers": summary.workers,
        "released_on_takeover": summary.released_on_takeover,
        "completed": summary.completed,
        "skipped": summary.skipped,
        "failed": summary.failed,
        "deferred": summary.deferred,
        "cancelled_midway": summary.cancelled_midway,
        "cascade_requeued": [
            {"stable_id": o.stable_id, "lane": o.lane, "reason": o.reason}
            for o in summary.cascade_outcomes
            if o.requeued
        ],
        "cascade_batch_id": summary.cascade_batch_id,
        "drain_batches": len(summaries),
    }
    if not args.json:
        console.print(
            f"{summary.batch_id}: completed {summary.completed}, skipped "
            f"{summary.skipped}, failed {summary.failed}, released on takeover "
            f"{summary.released_on_takeover}"
        )
    _emit(payload, as_json=args.json)
    return EXIT_TRACK_FAILURES if summary.failed else EXIT_OK


def cmd_version_bump(args: argparse.Namespace) -> int:
    conn = _conn(args.db)
    try:
        result = queue_api.requeue_for_version_bump(
            conn,
            backend=args.backend,
            current_version=args.version,
            lane=args.lane,
            resolve=queue_targets.candidates_from_state,
        )
    finally:
        conn.close()
    if result is None:
        if not args.json:
            console.print("nothing on an older producer version; no batch created")
        _emit({"batch_id": None, "requeued": 0}, as_json=args.json)
        return EXIT_OK
    if not args.json:
        console.print(
            f"{result.batch_id}: re-queued {result.admitted} track(s), refused "
            f"{result.refused}"
        )
    _emit(
        {
            "batch_id": result.batch_id,
            "requeued": result.admitted,
            "refused": result.refused,
            "workers": result.workers,
            "band": result.band,
        },
        as_json=args.json,
    )
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    conn = _conn(args.db)
    try:
        batches = queue_store.list_batches(conn, limit=args.limit)
        payload = {
            "batches": [
                {
                    "batch_id": b.batch_id,
                    "state": b.state,
                    "workers": b.workers,
                    "band": b.band,
                    "created_at": b.created_at,
                    "note": b.note,
                    "counts": queue_store.counts_by_state(conn, b.batch_id),
                }
                for b in batches
            ]
        }
    finally:
        conn.close()
    if not args.json:
        table = Table(title="analysis backfill batches")
        for column in ("batch_id", "state", "workers", "band", "created_at"):
            table.add_column(column)
        for b in batches:
            table.add_row(b.batch_id, b.state, str(b.workers), b.band, b.created_at)
        console.print(table)
    _emit(payload, as_json=args.json)
    return EXIT_OK


#-----------------------------------------------------------------------------
# entry point
#-----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.analysis.queue_cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", help="state.db path (default: the resolved data dir)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("enqueue", help="plan a batch from stable_ids")
    p.add_argument("--stable-id", action="append", required=True, dest="stable_id")
    p.add_argument("--lane", required=True)
    p.add_argument("--backend", required=True)
    p.add_argument("--note")
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("progress", help="aggregate + per-item progress")
    p.add_argument("--batch-id", required=True)
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_progress)

    p = sub.add_parser("cancel", help="cancel pending and in-flight items")
    p.add_argument("--batch-id", required=True)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("resume", help="put a cancelled or abandoned batch back")
    p.add_argument("--batch-id", required=True)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("run", help="drain a batch")
    p.add_argument("--batch-id", required=True)
    p.add_argument(
        "--backend",
        required=True,
        help="registry name, or an explicit module:ClassName import path",
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("version-bump", help="re-queue tracks on an old version")
    p.add_argument("--backend", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--lane", required=True)
    p.set_defaults(func=cmd_version_bump)

    p = sub.add_parser("list", help="recent batches")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)
    queue_user_cli.add_user_parsers(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (queue_api.QueueError, KeyError) as exc:
        console.print(f"[red][ERROR][/red] {exc}")
        return EXIT_USAGE
    except Exception:
        log.exception("queue command failed")
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
