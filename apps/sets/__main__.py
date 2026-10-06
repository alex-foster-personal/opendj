"""CLI entry for ``python -m apps.sets``.

Plan 12-01 surface: ``start``, ``stop``, ``resume``, ``status``,
``list``, ``prune``. Plans 12-02/12-03 extend this with ``classify``,
``label``, ``train``, ``replay``. ``rec`` drives the running app's REC over
HTTP, master mix included (SET-12).
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

from . import record as record_mod
from . import retention as retention_mod
from .classify import classify_session
from .classify import model as classify_model
from .label import label_session
from .paths import SessionPathError
from .rec_cli import add_parser as add_rec_parser
from .replay import replay as replay_cmd
from .soundcloud_export import (
    LICENSING_REMINDER,
    SessionNotFound,
    build_soundcloud_export,
)
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
        target = next(
            (s for s in sessions if s.session_id == args.session_id), None
        )
    else:
        target = active[0] if active else None
    if target is None:
        print("no active session", file=sys.stderr)
        return 2
    # Codex Phase 12 review finding: previously this only marked the DB
    # row ended and unlinked the pid, leaving ``manifest.json`` absent
    # and the recording unfinalisable for downstream tooling. Delegate
    # to :func:`record_mod.finalize` so the manifest is written, the
    # ``session_end`` event is appended to the timeline, the sets row
    # is marked ended, and the pid file is cleared.
    manifest = record_mod.finalize(target.session_id, state=state)
    print(f"ended session {manifest.session_id}")
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

    def _handler(signum: int, _frame: Any) -> None:  # pragma: no cover
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

    sp_classify = sub.add_parser(
        "classify", help="classify transitions for a session"
    )
    sp_classify.add_argument("session_id")
    sp_classify.add_argument("--force-rules", action="store_true")
    sp_classify.set_defaults(func=_dispatch_classify)

    sp_label = sub.add_parser("label", help="hand-label transitions")
    sp_label.add_argument("session_id")
    sp_label.add_argument("--relabel", action="store_true")
    sp_label.set_defaults(func=_dispatch_label)

    sp_train = sub.add_parser("train", help="train the transition classifier")
    sp_train.add_argument(
        "--sessions",
        nargs="+",
        default=None,
        help="session_ids to include; default = every session with labels.jsonl",
    )
    sp_train.set_defaults(func=_dispatch_train)

    sp_replay = sub.add_parser("replay", help="replay a recorded session")
    sp_replay.add_argument("session_id")
    sp_replay.add_argument("--format", choices=("rich", "jsonl"), default="rich")
    sp_replay.add_argument("--since", default=None)
    sp_replay.add_argument("--class", dest="class_filter", default=None)
    sp_replay.set_defaults(func=_dispatch_replay)

    sp_sc = sub.add_parser(
        "soundcloud-export",
        help="generate a paste-ready SoundCloud tracklist comment (metadata only)",
    )
    sp_sc.add_argument("session_id")
    sp_sc.add_argument(
        "--json",
        action="store_true",
        help="print the export object as JSON",
    )
    sp_sc.add_argument(
        "--acknowledge-rights",
        action="store_true",
        help="required to emit the paste-ready comment",
    )
    sp_sc.add_argument(
        "--out",
        default=None,
        help="write only the comment text to PATH (requires --acknowledge-rights)",
    )
    sp_sc.set_defaults(func=_dispatch_soundcloud_export)

    add_rec_parser(sub)

    return p


def _dispatch_soundcloud_export(args: argparse.Namespace) -> int:
    try:
        export = build_soundcloud_export(args.session_id)
    except SessionPathError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SessionNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not args.acknowledge_rights:
        if args.json:
            print(json.dumps(export.to_dict(include_comment=False), indent=2))
        else:
            print(LICENSING_REMINDER)
        return 2

    if args.out is not None:
        Path(args.out).write_text(export.comment, encoding="utf-8")

    if args.json:
        print(json.dumps(export.to_dict(include_comment=True), indent=2))
        return 0

    print(LICENSING_REMINDER)
    print()
    if export.comment:
        sys.stdout.write(export.comment)
        if not export.comment.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def _dispatch_replay(args: argparse.Namespace) -> int:
    return replay_cmd(
        args.session_id,
        output_format=args.format,
        since=args.since,
        class_filter=args.class_filter,
    )


def _dispatch_classify(args: argparse.Namespace) -> int:
    state = SetsState()
    out = classify_session(
        args.session_id,
        state=state,
        force_rules=args.force_rules,
    )
    print(f"wrote {out}")
    return 0


def _dispatch_label(args: argparse.Namespace) -> int:
    label_session(args.session_id, relabel=args.relabel)
    return 0


def _dispatch_train(args: argparse.Namespace) -> int:
    sessions = args.sessions
    if sessions is None:
        state = SetsState()
        sessions = [s.session_id for s in state.list_sessions()]
    report = classify_model.train(sessions)
    print(json.dumps({
        "n_training_sessions": report.n_training_sessions,
        "n_labeled_transitions": report.n_labeled_transitions,
        "macro_f1": report.macro_f1,
        "accepted": report.accepted,
        "model_path": str(report.model_path) if report.model_path else None,
    }, indent=2))
    return 0 if report.accepted else 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
