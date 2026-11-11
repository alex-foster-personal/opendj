"""CLI for the rekordbox-ingestion skill ((private path)).

    uv run python -m apps.rekordbox_ingestion <command> --batch <name> ...

Commands, in the order a batch goes through them:

  stage      gather files into ~/Music/_ingest/<batch>/ (clone copy, never move)
  playlists  pre-create the blank folder/playlists in master.db (GATED write)
  open       Finder on the batch + rekordbox, side by side
  status     read-only: MISSING / IMPORTED / ANALYZED per file, playlist counts
  watch      poll until every file is ANALYZED, then optionally quit rekordbox

Mutating commands need an explicit --dry-run or --live; there is no default.
Exit codes: 0 done, 1 error, 2 watch timed out, 3 needs the maintainer (AuthQueue),
4 `status --exit-code` and not every file is analyzed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from apps.rekordbox_ingestion import desktop, playlists, stage, status
from apps.rekordbox_ingestion.config import CFG
from apps.shared import platform_paths
from apps.shared.rekordbox_writeback import RekordboxWritebackDisabled

EXIT_TIMEOUT = 2
EXIT_NEEDS_MAINTAINER = 3
EXIT_NOT_COMPLETE = 4


# ----- helpers ---------------------------------------------------------------


def _batch_dir(args: argparse.Namespace) -> Path:
    if args.dir:
        return Path(args.dir).expanduser().resolve()
    if not args.batch or "/" in args.batch:
        raise SystemExit("[ERROR] give --batch <name> (no slashes) or --dir <path>")
    return CFG.BATCH_ROOT / args.batch


def _db_path(args: argparse.Namespace) -> Path:
    return Path(args.db).expanduser() if args.db else platform_paths.REKORDBOX_LIVE_DB


def _utc_stamp(fmt: str) -> str:
    return dt.datetime.now(dt.UTC).strftime(fmt)


def _write_report(kind: str, batch_dir: Path, payload: dict) -> Path:
    CFG.REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp("%Y%m%dT%H%M%SZ")
    out = CFG.REPORT_ROOT / f"{batch_dir.name}-{kind}-{stamp}.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out


def _summary_line(st: status.BatchStatus) -> str:
    c = st.counts
    return (
        f"denominator={st.denominator} audio files in {st.batch_dir} | "
        f"in rekordbox={st.in_rekordbox_count} | analyzed={c['ANALYZED']} | "
        f"missing={c['MISSING']} | in {CFG.PLAYLIST_FOLDER} playlists={st.in_playlist_count} | "
        f"playlist sizes={st.playlists}"
    )


def _desktop_idle() -> tuple[str, float]:
    return desktop.frontmost_app_name(), desktop.hid_idle_seconds()


def _print_status(st: status.BatchStatus, *, verbose: bool) -> None:
    print(_summary_line(st))
    in_rb = st.in_rekordbox_count
    if in_rb > st.in_playlist_count:
        print(f"[WARN] {in_rb - st.in_playlist_count} imported file(s) are in no {CFG.PLAYLIST_FOLDER} "
              "playlist (dropped on Collection instead of the playlist?)")
    unusual = [f for f in st.files if f.unusual_code]
    if unusual:
        print(f"[WARN] {len(unusual)} analyzed with an Analysed code other than "
              f"the library norm {CFG.LIBRARY_NORM_ANALYSED_CODE}: "
              f"{sorted({f.analysed for f in unusual if f.analysed is not None})}")
    for f in st.files:
        if verbose or f.state != "ANALYZED":
            extra = f" missing_anlz={f.missing_anlz}" if f.missing_anlz and f.state == "IMPORTED" else ""
            print(f"  {f.state:<8} {f.path}{extra}")


def _mode(args: argparse.Namespace) -> str:
    if args.dry_run == args.live:
        raise SystemExit("[ERROR] pass exactly one of --dry-run or --live")
    return "dry-run" if args.dry_run else "live"


# ----- commands --------------------------------------------------------------


def cmd_stage(args: argparse.Namespace) -> int:
    mode = _mode(args)
    batch_dir = _batch_dir(args)
    if args.from_list:
        sources = stage.read_source_list(Path(args.from_list).expanduser())
    elif args.from_dir:
        sources = stage.sources_from_dir(Path(args.from_dir).expanduser())
    else:
        raise SystemExit("[ERROR] stage needs --from-list <tsv> or --from-dir <dir>")
    plan = stage.plan_stage(sources, batch_dir)
    print(f"[{mode}] batch {batch_dir}: copy {len(plan.copy)}, already there {len(plan.already_there)}")
    for source, destination in plan.copy:
        print(f"  {source} -> {destination}")
    if mode == "live":
        stage.execute_stage(plan)
        print(f"[OK] staged {len(plan.copy)} files; sources untouched")
    return 0


def cmd_playlists(args: argparse.Namespace) -> int:
    mode = _mode(args)
    batch_dir = _batch_dir(args)
    names = args.playlist or [batch_dir.name]
    db_path = _db_path(args)
    processes = playlists.running_rekordbox_processes()
    holders = playlists.db_set_holders(db_path)
    print(f"[{mode}] db={db_path} folder={args.folder!r} playlists={names}")
    print(f"  rekordbox processes={processes or 'none'} db holders={holders or 'none'}")
    if mode == "dry-run":
        with status.snapshot_db(db_path) as snapshot:
            plan = playlists.plan_blank_playlists(snapshot, args.folder, names)
        print(f"  folder exists={plan.folder_exists} would create={plan.to_create} "
              f"already present (name: members)={plan.already_present}")
        print(f"  would back up the DB set to {Path(args.backup_root)}/<UTC stamp>-rekordbox-ingestion/")
        if processes or holders:
            print("[WARN] a --live run would REFUSE now: rekordbox is open. Quit it first.")
        return 0
    backup, plan, ids = playlists.create_blank_playlists(
        db_path, args.folder, names, backup_root=Path(args.backup_root)
    )
    if backup is None:
        print("[OK] nothing to create: every playlist already exists; no backup taken, DB not opened")
    else:
        print(f"[OK] backup: {backup.directory} ({len(backup.files)} files, sha256 verified)")
    print(f"[OK] created={plan.to_create} already present={plan.already_present}")
    print(f"[OK] verified on re-read (integrity ok, XML entries present): {ids}")
    report = _write_report("playlists", batch_dir, {
        "db": str(db_path), "backup": str(backup.directory) if backup else None, "folder": args.folder,
        "created": plan.to_create, "already_present": plan.already_present, "ids": ids,
    })
    print(f"[OK] report: {report}")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    mode = _mode(args)
    for line in desktop.open_side_by_side(_batch_dir(args), dry_run=mode == "dry-run"):
        print(line)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    st = status.read_batch_status(_batch_dir(args), db_path=_db_path(args), folder=args.folder)
    if args.json:
        print(json.dumps(st.to_dict(), indent=2))
    else:
        _print_status(st, verbose=args.verbose)
        print("[OK] complete: every file analyzed" if st.complete else "[..] not complete")
    return EXIT_NOT_COMPLETE if args.exit_code and not st.complete else 0


def _poll(batch_dir: Path, db_path: Path, folder: str) -> status.BatchStatus | None:
    try:
        return status.read_batch_status(batch_dir, db_path=db_path, folder=folder)
    except status.SnapshotUnreadable as exc:
        print(f"[WARN] snapshot unreadable this poll: {exc}")
        return None


def cmd_watch(args: argparse.Namespace) -> int:
    mode = _mode(args)
    batch_dir, db_path = _batch_dir(args), _db_path(args)
    tracker = desktop.TouchTracker(idle_minutes=args.idle_min, started_at=time.time())
    if mode == "dry-run":
        st = _poll(batch_dir, db_path, args.folder)
        if st is None:
            return 1
        _print_status(st, verbose=False)
        front, idle = _desktop_idle()
        print(f"[dry-run] frontmost={front!r} input idle={idle:.0f}s; "
              f"would poll every {args.interval}s for up to {args.timeout_min} min")
        print(f"[dry-run] complete now={st.complete}; on completion would "
              + ("quit rekordbox after "
                 f"{args.idle_min} min without the maintainer touching it" if args.quit_when_done else "NOT quit rekordbox"))
        return 0
    deadline = time.time() + args.timeout_min * 60
    unreadable = stable = 0
    last_counts: dict[str, int] | None = None
    while True:
        now = time.time()
        front, idle = _desktop_idle()
        tracker.observe(now, front, idle, args.interval)
        st = _poll(batch_dir, db_path, args.folder)
        if st is None:
            unreadable += 1
            if unreadable >= CFG.MAX_UNREADABLE_POLLS:
                print(f"[ERROR] {unreadable} unreadable snapshots in a row; giving up")
                return 1
        else:
            unreadable = 0
            stamp = _utc_stamp("%H:%M:%SZ")
            print(f"{stamp} {_summary_line(st)}", flush=True)
            stable = stable + 1 if st.complete and st.counts == last_counts else (1 if st.complete else 0)
            last_counts = st.counts
            if stable >= 2:
                return _finish(args, batch_dir, db_path, st, tracker)
        if now > deadline:
            print(f"[ERROR] timed out after {args.timeout_min} min; not complete")
            return EXIT_TIMEOUT
        time.sleep(args.interval)


def _finish(args, batch_dir: Path, db_path: Path, st: status.BatchStatus, tracker) -> int:
    print("[OK] every file analyzed in two consecutive polls")
    _print_status(st, verbose=False)
    payload: dict = {"status": st.to_dict(), "quit": "not requested"}
    code = 0
    if args.quit_when_done:
        if not playlists.running_rekordbox_processes():
            payload["quit"] = "rekordbox already closed"
        else:
            while True:
                now = time.time()
                front, idle = _desktop_idle()
                tracker.observe(now, front, idle, args.interval)
                ok, reason = tracker.may_quit(now, front, idle)
                if ok:
                    break
                if now - tracker.started_at > args.timeout_min * 60:
                    print(f"[NEEDS-MAINTAINER] analysis done but not quitting: {reason}. Ask via AuthQueue.")
                    payload["quit"] = f"skipped: {reason}"
                    _write_report("watch", batch_dir, payload)
                    return EXIT_NEEDS_MAINTAINER
                print(f"[..] not quitting yet: {reason}", flush=True)
                time.sleep(args.interval)
            print(f"[..] quitting rekordbox: {reason}")
            if not desktop.quit_rekordbox_gracefully():
                print("[NEEDS-MAINTAINER] rekordbox did not exit after quit (a dialog is probably open). "
                      "Not force-killing. Ask via AuthQueue.")
                payload["quit"] = "quit sent, process still running"
                _write_report("watch", batch_dir, payload)
                return EXIT_NEEDS_MAINTAINER
            payload["quit"] = f"quit gracefully: {reason}"
            final = status.read_batch_status(batch_dir, db_path=db_path, folder=args.folder)
            print(f"[OK] after quit, re-read of the closed DB: {_summary_line(final)}")
            payload["status_after_quit"] = final.to_dict()
            if not final.complete:
                print("[ERROR] the closed DB does not show every file analyzed")
                code = 1
    print(f"[OK] report: {_write_report('watch', batch_dir, payload)}")
    return code


# ----- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.rekordbox_ingestion", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, mode: bool, db: bool = True) -> None:
        p.add_argument("--batch", help=f"batch name under {CFG.BATCH_ROOT}")
        p.add_argument("--dir", help="explicit batch dir instead of --batch")
        p.add_argument("--folder", default=CFG.PLAYLIST_FOLDER, help="rekordbox playlist folder")
        if db:
            p.add_argument("--db", help="rekordbox master.db (default: the live one)")
        if mode:
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--live", action="store_true")

    p = sub.add_parser("stage", help="gather files into the batch dir")
    common(p, mode=True, db=False)
    p.add_argument("--from-list", help="TSV/list file; first column is a path")
    p.add_argument("--from-dir", help="copy every audio file under this dir")
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("playlists", help="pre-create blank playlists (gated write)")
    common(p, mode=True)
    p.add_argument("--playlist", action="append", help="playlist name (repeatable; default: batch name)")
    p.add_argument("--backup-root", default=str(CFG.BACKUP_ROOT))
    p.set_defaults(func=cmd_playlists)

    p = sub.add_parser("open", help="Finder on the batch + rekordbox, side by side")
    common(p, mode=True, db=False)
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("status", help="read-only per-file import/analysis state")
    common(p, mode=False)
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", action="store_true", help="list analyzed files too")
    p.add_argument("--exit-code", action="store_true", help="exit 4 when not every file is analyzed")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help="poll until analyzed; optionally quit rekordbox")
    common(p, mode=True)
    p.add_argument("--interval", type=int, default=CFG.POLL_SECONDS)
    p.add_argument("--timeout-min", type=int, default=CFG.TIMEOUT_MINUTES)
    p.add_argument("--idle-min", type=float, default=CFG.IDLE_MINUTES_BEFORE_QUIT)
    p.add_argument("--quit-when-done", action="store_true")
    p.set_defaults(func=cmd_watch)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (
        RekordboxWritebackDisabled, playlists.RekordboxNotClosed, playlists.VerificationFailed,
        status.SnapshotUnreadable, FileNotFoundError, ValueError,
    ) as exc:
        # Expected refusals: one clear line, exit 1. Anything else keeps its traceback.
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
