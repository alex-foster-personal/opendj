"""Command-line surface for DJ Copilot (Phase 13).

Subcommands: ``play-it`` + ``suggest-next``. Exit codes 0/1/2.
No network, no live-DB writes.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from apps.shared.harmonic import TrackFeature
from apps.shared.paths import STATE_DB
from apps.shared.play_orders.schema import apply_play_order_migrations
from apps.shared.state import db as state_db

from .pinning import PinUnsatisfiableError
from .play_it import InsufficientDataError, play_it
from .session_context import PlayedTrack, SessionContext, load_session_context
from .set_goal import SetGoal
from .suggester import suggest_next


def _open_conn(db: Path) -> sqlite3.Connection:
    """Open the state DB via Phase 5's :func:`open_rw` and layer the
    play-order schema on top.

    Routing through :func:`apps.shared.state.db.open_rw` ensures the
    Phase 5 WAL + busy_timeout + foreign_keys pragmas and core schema
    migrations run first; :func:`apply_play_order_migrations` then
    creates the additive play-order tables idempotently.
    """
    conn = state_db.open_rw(db)
    apply_play_order_migrations(conn)
    return conn


def _load_tracks_from_json(path: Path) -> list[TrackFeature]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        TrackFeature(
            stable_id=str(row["stable_id"]),
            artist=row.get("artist"),
            bpm=(None if row.get("bpm") is None else float(row["bpm"])),
            key_camelot=row.get("key_camelot") or row.get("key"),
            energy=(None if row.get("energy") is None else int(row["energy"])),
        )
        for row in raw
    ]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dj-copilot", description=__doc__)
    p.add_argument("--db", type=Path, default=STATE_DB)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("play-it")
    pi.add_argument("--playlist", required=True)
    pi.add_argument("--tracks-json", type=Path, required=True)
    pi.add_argument("--duration", type=int, required=True)
    pi.add_argument("--peak-at", type=int, default=None)
    pi.add_argument("--floor", type=int, default=3)
    pi.add_argument("--ceiling", type=int, default=9)
    pi.add_argument("--open-on", default=None)
    pi.add_argument("--close-on", type=int, default=None)
    pi.add_argument("--name", default="PLAY IT")
    pi.add_argument("--overwrite", action="store_true")
    pi.add_argument("--peak-pin", action="append", default=[], dest="peak_pins")
    pi.add_argument("--opener-pin", action="append", default=[], dest="opener_pins")
    pi.add_argument("--closer-pin", default=None)

    sn = sub.add_parser("suggest-next")
    sn.add_argument("--current", required=True)
    sn.add_argument("--library-json", type=Path, required=True)
    sn.add_argument("--top", type=int, default=10)
    sn.add_argument(
        "--source",
        default="manual",
        choices=["auto", "phase12", "rekordbox_history", "djay_history", "manual"],
    )
    sn.add_argument("--session-json", type=Path, default=None)
    sn.add_argument("--explain", action="store_true")
    return p


def _cmd_play_it(args: argparse.Namespace) -> int:
    try:
        goal = SetGoal(
            duration_min=args.duration,
            peak_at_min=args.peak_at,
            floor_energy=args.floor,
            ceiling_energy=args.ceiling,
            open_on_key=args.open_on,
            close_on_energy=args.close_on,
            peak_pins=tuple(args.peak_pins),
            opener_pins=tuple(args.opener_pins),
            closer_pin=args.closer_pin,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        tracks = _load_tracks_from_json(args.tracks_json)
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
        print(f"error: failed to load tracks-json: {exc}", file=sys.stderr)
        return 2

    conn = _open_conn(args.db)
    try:
        try:
            po_id, result = play_it(
                conn=conn,
                playlist_id=args.playlist,
                goal=goal,
                tracks=tracks,
                name=args.name,
                overwrite=args.overwrite,
            )
        except InsufficientDataError as exc:
            print(f"pre-flight failed: {exc}", file=sys.stderr)
            return 1
        except PinUnsatisfiableError as exc:
            print(f"error: {exc.reason}: {exc}", file=sys.stderr)
            return 2
        has_roles = any(
            getattr(step, "pin_role", None) for step in result.per_step_trace
        )
        print(
            f"PLAY IT stored: play_order_id={po_id}, "
            f"tracks={len(result.order)}, solve_ms={result.solve_ms:.2f}"
        )
        if has_roles:
            print(f"{'#':>3}  {'stable_id':<40}  {'hint':<20}  {'role':<8}")
        else:
            print(f"{'#':>3}  {'stable_id':<40}  {'hint':<20}")
        for i, sid in enumerate(result.order):
            hint = (
                result.per_step_trace[i].transition_hint
                if i < len(result.per_step_trace)
                else ""
            )
            if has_roles:
                role = (
                    result.per_step_trace[i].pin_role or ""
                    if i < len(result.per_step_trace)
                    else ""
                )
                print(f"{i:>3}  {sid:<40}  {hint:<20}  {role:<8}")
            else:
                print(f"{i:>3}  {sid:<40}  {hint:<20}")
        if result.constraints_unmet:
            print(
                f"\nconstraints_unmet ({len(result.constraints_unmet)}):",
                file=sys.stderr,
            )
            for unmet in result.constraints_unmet:
                print(
                    f"  [{unmet.position:>3}] {unmet.kind} {unmet.detail}",
                    file=sys.stderr,
                )
        return 0
    finally:
        conn.close()


def _cmd_suggest_next(args: argparse.Namespace) -> int:
    try:
        library = _load_tracks_from_json(args.library_json)
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
        print(f"error: failed to load library-json: {exc}", file=sys.stderr)
        return 2

    context: SessionContext | None = None
    manual_recent: list[PlayedTrack] | None = None
    if args.session_json is not None:
        raw = json.loads(args.session_json.read_text(encoding="utf-8"))
        now = datetime.now(UTC)
        manual_recent = [
            PlayedTrack(
                stable_id=row["stable_id"],
                artist=row.get("artist"),
                bpm=row.get("bpm"),
                key_camelot=row.get("key_camelot") or row.get("key"),
                energy=row.get("energy"),
                played_at=now,
            )
            for row in raw
        ]
        if args.source == "manual":
            context = SessionContext(
                recent=manual_recent,
                source="manual",
                captured_at=now,
            )

    conn = _open_conn(args.db)
    try:
        if context is None:
            # Honor --source: auto/phase12/rekordbox_history/djay_history
            # use the loader (which can read from state DB); manual with
            # no session-json yields an empty manual context.
            context = load_session_context(
                conn=conn,
                source=args.source,
                manual=manual_recent,
            )
        suggestions = suggest_next(
            conn=conn,
            current_stable_id=args.current,
            library=library,
            context=context,
            top_n=args.top,
            explain=args.explain,
        )
        print(
            f"{'#':>3}  {'stable_id':<40}  {'score':>6}  {'rationale':<40}"
        )
        for i, sug in enumerate(suggestions):
            tags = ",".join(sug.rationale_tags)
            print(
                f"{i:>3}  {sug.stable_id:<40}  "
                f"{sug.score:>6.3f}  {tags:<40}"
            )
            if sug.explain_text:
                print(f"       explain: {sug.explain_text}")
        return 0
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "play-it":
        return _cmd_play_it(args)
    if args.cmd == "suggest-next":
        return _cmd_suggest_next(args)
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
