"""Command-line entrypoint for the Spotify importer + rematch tools.

Usage (dry-run first, always):

    doppler run -p construct -c dev_af -- \
        python -m apps.spotify import <URL>

``--live`` requires BOTH ``--i-understand-the-risks`` AND a typed
playlist-id confirmation at the prompt. Mirrors the Phase 1 safety
rails (``apps/reconcile/apply.py``).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from apps.shared.state import paths as state_paths

from .client import MissingCredentialsError, SpotifyClient
from .listen_history import fetch_recent_plays
from .importer import (
    EXIT_API_ERROR,
    EXIT_OK,
    EXIT_SAFETY,
    EXIT_UNMATCHED,
    run_import,
)
from .rematch import rematch_playlist
from .state_writer import (
    backup_state_db,
    emit_reversal_script,
    open_state_rw_with_aux,
)
from .url_parse import parse_playlist_identifier
from .watched import ensure_watched_defaults, watched_ids


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.spotify",
        description="Spotify playlist importer + acquisition queue (Phase 9 / CAT-01).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_imp = sub.add_parser(
        "import", help="fetch a Spotify playlist, match, and optionally write"
    )
    p_imp.add_argument("url_or_id", help="Spotify playlist URL / URI / id.")
    p_imp.add_argument("--live", action="store_true",
                       help="Write to the shared-state DB.")
    p_imp.add_argument("--i-understand-the-risks", action="store_true",
                       help="Required alongside --live.")
    p_imp.add_argument("--max-tracks", type=int, default=10,
                       help="Cautious-run cap on imported tracks "
                            "(default: 10). Use 0 or --unlimited to "
                            "disable the cap.")
    p_imp.add_argument("--unlimited", action="store_true",
                       help="Disable the cautious --max-tracks cap "
                            "(equivalent to --max-tracks 0).")
    p_imp.add_argument("--bulk", action="store_true",
                       help="Lift the cautious cap (alias for --unlimited).")
    p_imp.add_argument("--force", action="store_true",
                       help="Ignore the snapshot_id short-circuit.")
    p_imp.add_argument("--no-cache", action="store_true",
                       help="Bypass the 24 h HTTP cache.")
    p_imp.add_argument("--out-dir", type=Path, default=None,
                       help="Override artifact root (default (private path)/).")

    p_rm = sub.add_parser(
        "rematch",
        help="rerun matching against the pending_tracks table for a playlist",
    )
    p_rm.add_argument("--playlist-id", required=True,
                      help="Spotify playlist URL / URI / id.")
    p_rm.add_argument("--live", action="store_true",
                      help="Promote resolved tracks into playlist_memberships.")
    p_rm.add_argument("--i-understand-the-risks", action="store_true",
                      help="Required alongside --live.")

    p_recent = sub.add_parser(
        "recent",
        help="live listen history, scored owned / review / unmatched",
    )
    p_recent.add_argument("--limit", type=int, default=50,
                          help="Plays to fetch, 1..50 (Spotify's own cap).")
    p_recent.add_argument("--unowned-only", action="store_true",
                          help="Only plays the local library does not have.")
    p_recent.add_argument("--json", action="store_true",
                          help="Emit JSON rows instead of the text table.")

    p_watch = sub.add_parser(
        "watched",
        help="list / ensure durable watched Spotify playlist URLs",
    )
    p_watch.add_argument(
        "--ensure",
        action="store_true",
        help="Write defaults if missing (idempotent).",
    )

    p_login = sub.add_parser(
        "login",
        help="sign in once so Open DJ can list all your playlists",
    )
    p_login.add_argument("--no-browser", action="store_true",
                         help="Print the sign-in URL instead of opening it.")

    return parser


def _cmd_login(args: argparse.Namespace) -> int:
    import time
    import webbrowser

    from .account import ClientIdUnreadable, ConnectFlow, NotConnected

    flow = ConnectFlow()
    try:
        url = flow.start()
    except NotConnected as exc:
        print(f"ERROR: {exc.message}", file=sys.stderr)
        return EXIT_SAFETY
    except ClientIdUnreadable as exc:
        print(f"ERROR: SPOTIFY_CLIENT_ID_UNREADABLE: {exc}", file=sys.stderr)
        return EXIT_SAFETY
    except OSError as exc:
        print(f"ERROR: could not listen for the Spotify redirect: {exc}", file=sys.stderr)
        return EXIT_API_ERROR
    print(f"Sign in to Spotify:\n  {url}")
    if not args.no_browser:
        webbrowser.open(url)
    while (status := flow.status())["phase"] == "waiting":
        time.sleep(0.5)
    if status["phase"] != "connected":
        print(f"ERROR: Spotify sign-in failed: {status['detail']}", file=sys.stderr)
        return EXIT_API_ERROR
    print("[spotify] signed in; Open DJ can now list all your playlists")
    return EXIT_OK


def _assert_live_safety(args: argparse.Namespace) -> int:
    if not args.live:
        return EXIT_OK
    if not args.i_understand_the_risks:
        print(
            "ERROR: --live requires --i-understand-the-risks.",
            file=sys.stderr,
        )
        return EXIT_SAFETY
    return EXIT_OK


def _cmd_import(args: argparse.Namespace) -> int:
    try:
        playlist_id = parse_playlist_identifier(args.url_or_id)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_SAFETY

    rc = _assert_live_safety(args)
    if rc != EXIT_OK:
        return rc

    # Safe-default policy (CAT-02): --max-tracks has a conservative cap by
    # default. Only --unlimited, --bulk, or an explicit --max-tracks 0 lift
    # the cap. A bare ``spotify import ...`` must never import every track
    # in a playlist against the live state DB.
    if args.bulk or args.unlimited or args.max_tracks == 0:
        max_tracks = None
    else:
        max_tracks = args.max_tracks

    if args.live:
        print(
            f"LIVE write requested for playlist {playlist_id}.\n"
            "This will (1) snapshot the state DB, (2) write a new Spotify "
            "playlist + pending rows, (3) emit a reversal script. "
            "Type the playlist id to confirm:"
        )
        try:
            typed = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\naborted.", file=sys.stderr)
            return EXIT_SAFETY
        if typed != playlist_id:
            print(
                f"typed confirmation mismatch; aborting.",
                file=sys.stderr,
            )
            return EXIT_SAFETY

    try:
        client = SpotifyClient.from_env()
    except MissingCredentialsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_API_ERROR

    try:
        from apps.spotify.importer import ImportOptions

        run = run_import(
            playlist_id,
            client=client,
            options=ImportOptions(
                live=args.live,
                force=args.force,
                use_cache=not args.no_cache,
                max_tracks=max_tracks,
                out_root=args.out_dir,
                progress=lambda msg: print(f"[spotify] {msg}"),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: import failed: {exc}", file=sys.stderr)
        return EXIT_API_ERROR

    _print_summary(run)

    if run.result.unmatched or run.result.review:
        return EXIT_UNMATCHED
    return EXIT_OK


def _cmd_rematch(args: argparse.Namespace) -> int:
    try:
        vendor_pl_id = parse_playlist_identifier(args.playlist_id)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_SAFETY
    playlist_id = f"spotify:{vendor_pl_id}"

    rc = _assert_live_safety(args)
    if rc != EXIT_OK:
        return rc

    if args.live:
        print(
            f"LIVE rematch requested for playlist {vendor_pl_id}.\n"
            "This will (1) snapshot the state DB, (2) promote resolved "
            "pending tracks into playlist_memberships, (3) emit a reversal "
            "script. Type the playlist id to confirm:"
        )
        try:
            typed = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\naborted.", file=sys.stderr)
            return EXIT_SAFETY
        if typed != vendor_pl_id:
            print(
                "typed confirmation mismatch; aborting.",
                file=sys.stderr,
            )
            return EXIT_SAFETY

        backup = backup_state_db()
        reversal = emit_reversal_script(backup)
        print(f"[spotify] backup: {backup}")
        print(f"[spotify] reversal script: {reversal}")

    conn = open_state_rw_with_aux()
    try:
        outcome = rematch_playlist(conn, playlist_id, live=args.live)
    finally:
        conn.close()

    print(
        f"[spotify] rematch {playlist_id}: "
        f"resolved={outcome.resolved_count} "
        f"still_pending={outcome.still_pending_count} "
        f"abandoned={outcome.abandoned_before} "
        f"live={'yes' if args.live else 'no (dry-run)'}"
    )
    if outcome.still_pending:
        return EXIT_UNMATCHED
    return EXIT_OK


def _print_summary(run) -> None:
    p = run.playlist
    r = run.result
    print("")
    print(f"Playlist: {p.name}  ({p.id})  snapshot={p.snapshot_id}")
    print(
        f"  total={len(r.pairs)}  matched={len(r.matched)}  "
        f"review={len(r.review)}  unmatched={len(r.unmatched)}  "
        f"match_rate={r.match_rate:.1%}"
    )
    print(f"  runtime: {run.runtime_seconds:.2f}s")
    print(f"  artifacts: {run.reports.root}")
    if run.write_summary is not None:
        ws = run.write_summary
        if ws.skipped_existing_snapshot:
            print("  state: up-to-date (snapshot unchanged); no writes performed")
            if ws.odj_playlist_id:
                print(f"  odj link: {ws.odj_playlist_id}")
        else:
            print(
                f"  state: wrote playlist_id={ws.playlist_id}  "
                f"matched={ws.matched_written}  pending={ws.pending_written}  "
                f"synthetic={ws.synthetic_tracks_written}  "
                f"vendor_ids={ws.vendor_ids_set}  "
                f"skipped: deleted by user={ws.tracks_skipped_deleted}"
            )
            if ws.odj_playlist_id:
                created = "created" if ws.odj_created else "linked"
                print(f"  odj ({created}): {ws.odj_playlist_id}")
            print(f"  backup: {ws.backup_path}")
            print(f"  reversal: {ws.reversal_script_path}")


def _cmd_watched(args: argparse.Namespace) -> int:
    if args.ensure:
        entries = ensure_watched_defaults()
        print(f"[spotify] watched registry ensured ({len(entries)} playlists)")
    else:
        entries = ensure_watched_defaults()
    for e in entries:
        label = f"  ({e.label})" if e.label else ""
        print(f"{e.id}  {e.url}{label}")
    print(f"ids: {', '.join(watched_ids())}")
    return EXIT_OK


def _cmd_recent(args: argparse.Namespace) -> int:
    """CLI twin of GET /api/v1/spotify/recently-played (agent-native parity)."""
    try:
        client = SpotifyClient.from_env()
    except MissingCredentialsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_SAFETY
    try:
        plays = fetch_recent_plays(client, state_paths.STATE_DB, limit=args.limit)
    except Exception as exc:  # noqa: BLE001 -- surfaced verbatim, never swallowed
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_API_ERROR

    if args.unowned_only:
        plays = [play for play in plays if play.ownership != "owned"]

    if args.json:
        print(json.dumps([asdict(play) for play in plays], indent=2, ensure_ascii=False))
        return EXIT_OK

    if not plays:
        print("[spotify] no plays returned for this account in Spotify's recent window")
        return EXIT_OK
    for play in plays:
        mark = {"owned": "OWNED ", "review": "REVIEW", "unmatched": "BUY   "}[play.ownership]
        print(f"{play.played_at}  {mark}  {play.artist} - {play.title}")
        if play.ownership != "owned":
            print(f"    beatport: {play.suggested_sources['beatport']}")
    unowned = sum(1 for play in plays if play.ownership == "unmatched")
    print(f"[spotify] {len(plays)} plays, {unowned} not in the local library")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "import":
        return _cmd_import(args)
    if args.command == "rematch":
        return _cmd_rematch(args)
    if args.command == "recent":
        return _cmd_recent(args)
    if args.command == "watched":
        return _cmd_watched(args)
    if args.command == "login":
        return _cmd_login(args)
    parser.print_help()
    return EXIT_SAFETY


if __name__ == "__main__":
    raise SystemExit(main())
