"""CLI entry for ``python -m apps.sets``.

Plan 12-01 surface: ``start``, ``stop``, ``resume``, ``status``,
``list``, ``prune``. Plans 12-02/12-03 extend this with ``classify``,
``label``, ``train``, ``replay``.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

from . import paths as sets_paths
from . import record as record_mod
from . import retention as retention_mod
from .state import SetsState


def _dispatch_start(args: argparse.Namespace) -> int:
    cfg = record_mod.RecorderConfig(
        sources=tuple(args.sources),
        capture_disabled=args.no_capture,
    )
    recorder = record_mod.start(session_id=args.session_id, config=cfg)
    print(f"started session {recorder.session_id}")
    if args.run_threads:
        _run_forever(recorder)
    return 0


def _dispatch_stop(args: argparse.Namespace) -> int:
    state = SetsState()
    sessions = state.list_sessions()
    active = [s for s in sessions if s.ended_at is None]
    if args.session_id:
        target = next((s for s in sessions if s.session_id == args.session_id), None)
    else:
        target = active[0] if active else None
    if target is None:
        print("no active session", file=sys.stderr)
        return 2
    # Stop is two things: (1) rm pid so status is clean; (2) mark ended.
    # Actually stopping a live ffmpeg subprocess belongs to the process
    # that owns it; a crash-recovery CLI stop is a best-effort finaliser.
    state.end_session(target.session_id)
    sess_dir = sets_paths.session_dir(target.session_id)
    pid_path = sess_dir / "recorder.pid"
    if pid_path.exists():
        pid_path.unlink()
    print(f"ended session {target.session_id}")
    return 0


def _dispatch_resume(args: argparse.Namespace) -> int:
    cfg = record_mod.RecorderConfig(
        sources=tuple(args.sources),
        capture_disabled=args.no_capture,
    )
    recorder = record_mod.resume(args.session_id, config=cfg)
    print(f"resumed session {recorder.session_id}")
    if args.run_threads:
        _run_forever(recorder)
    return 0


def _dispatch_status(_: argparse.Namespace) -> int:
    out = record_mod.status()
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def _dispatch_list(_: argparse.Namespace) -> int:
    state = SetsState()
    rows = state.list_sessions()
    if not rows:
        print("no sessions recorded yet")
        return 0
    for r in rows:
        ended = r.ended_at or "(active)"
        print(
            f"{r.session_id}  start={r.started_at}  end={ended}  "
            f"device={r.capture_device}  share={r.share_state}"
        )
    return 0


def _dispatch_prune(args: argparse.Namespace) -> int:
    candidates = retention_mod.prune(
        retention_days=args.retention_days,
        dry_run=not args.apply,
    )
    if not candidates:
        print("no segments older than retention window")
        return 0
    total = sum(c.size_bytes for c in candidates)
    mode = "WOULD DELETE" if not args.apply else "DELETED"
    for c in candidates:
        print(f"{mode}  {c.path}  ({c.size_bytes} B, {c.age_days:.1f} days)")
    print(f"-- {mode}: {len(candidates)} files / {total} B total")
    return 0


def _run_forever(recorder: record_mod.Recorder) -> None:
    """Block until SIGTERM/SIGINT, running poll threads in the background."""
    recorder.start_threads()

    def _handler(signum: int, frame: Any) -> None:  # pragma: no cover
        print(f"\nreceived signal {signum}; shutting down")
        record_mod.stop(recorder)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    try:
        while True:
            time.sleep(1.0)
    except SystemExit:
        raise
    except Exception:  # pragma: no cover
        record_mod.stop(recorder)
        raise


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m apps.sets")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_start = sub.add_parser("start", help="start a new recording session")
    sp_start.add_argument("--session-id", default=None)
    sp_start.add_argument(
        "--sources",
        nargs="+",
        default=["djay_monitor"],
        help="deck-state sources to enable",
    )
    sp_start.add_argument(
        "--no-capture",
        action="store_true",
        help="skip the ffmpeg capture subprocess (timeline only)",
    )
    sp_start.add_argument(
        "--run-threads",
        action="store_true",
        help="block foreground and run polling threads",
    )
    sp_start.set_defaults(func=_dispatch_start)

    sp_stop = sub.add_parser("stop", help="stop the active recording session")
    sp_stop.add_argument("--session-id", default=None)
    sp_stop.set_defaults(func=_dispatch_stop)

    sp_resume = sub.add_parser("resume", help="resume a previously-started session")
    sp_resume.add_argument("session_id")
    sp_resume.add_argument("--sources", nargs="+", default=["djay_monitor"])
    sp_resume.add_argument("--no-capture", action="store_true")
    sp_resume.add_argument("--run-threads", action="store_true")
    sp_resume.set_defaults(func=_dispatch_resume)

    sub.add_parser("status", help="print the active session, if any").set_defaults(
        func=_dispatch_status
    )
    sub.add_parser("list", help="list recorded sessions").set_defaults(
        func=_dispatch_list
    )

    sp_prune = sub.add_parser("prune", help="prune old MP3 segments")
    sp_prune.add_argument("--retention-days", type=int, default=90)
    sp_prune.add_argument("--apply", action="store_true")
    sp_prune.set_defaults(func=_dispatch_prune)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
