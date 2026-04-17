"""Safe apply pass for Phase 3 playlist sync (SYNC-03).

Consumes ``data/sync/playlist-plan.json`` produced by
``apps.sync.playlist_diff`` and writes the RB-canonical diff into djay's
``MediaLibrary.db`` under the six-rail safety pattern established in
Phase 1 (``apps/reconcile/remove_track.py``, ``apps/reconcile/apply.py``):

1. Typed confirmation (``APPLY PLAYLIST SYNC``).
2. ``pgrep`` gate: abort if djay OR Rekordbox is running.
3. Timestamped DB backup.
4. Atomic ``BEGIN IMMEDIATE ... COMMIT`` per playlist op.
5. Post-write readback verification (membership matches plan).
6. Reversal script that restores the backup with a big CloudKit warning.

Entry point::

    python -m apps.sync.playlist_apply [--plan PATH] \
        [--dry-run | --live] [--i-understand-the-risks] \
        [--playlists "NAME1,NAME2" | --bulk] [--backup-dir PATH]

Default mode is ``--dry-run``; ``--live`` is gated behind
``--i-understand-the-risks``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sqlite3
import subprocess
import sys
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path

from apps.shared import paths
from apps.sync import playlist_tsaf as ptsaf


CONFIRMATION_PHRASE = "APPLY PLAYLIST SYNC"
DEFAULT_PLAN: Path = paths.DATA_DIR / "sync" / "playlist-plan.json"
DEFAULT_BACKUP_DIR: Path = paths.DATA_DIR / "sync" / "backups"
CLOUDKIT_WARNING = (
    "CloudKit overwrite risk: djay will re-upload the modified playlist "
    "blob(s) on next launch. If iCloud has a newer copy it may win. See "
    "docs/djay_db_schema_v2.md section 6. Keep djay quit until the "
    "reversal script has been run (if needed)."
)


class PlaylistApplyError(RuntimeError):
    """Raised by the apply pipeline on any abortable condition."""


# ----- Path resolution (Phase 1.1 gap) ----------------------------------


def _live_db_path(live: bool) -> Path:
    """Return the djay DB path we will open.

    ``--live`` MUST resolve to :data:`paths.DJAY_LIVE_DB`; otherwise we
    open the working copy under ``data/``.
    """
    return paths.DJAY_LIVE_DB if live else paths.DJAY_WORKING_DB


# ----- Safety rails -----------------------------------------------------


def _process_running(pattern: str) -> bool:
    """True if ``pgrep -if <pattern>`` finds any match."""
    try:
        r = subprocess.run(
            ["pgrep", "-if", pattern],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        sys.stderr.write(
            f"[warn] pgrep not available; cannot verify {pattern!r} is not running\n"
        )
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _assert_djay_quit() -> None:
    if _process_running("djay"):
        raise PlaylistApplyError(
            "djay is currently running. Quit djay before applying the plan."
        )


def _assert_rekordbox_quit() -> None:
    if _process_running("rekordbox"):
        raise PlaylistApplyError(
            "Rekordbox is currently running. Quit Rekordbox before applying the plan."
        )


def _typed_confirm(expected: str = CONFIRMATION_PHRASE) -> bool:
    """Block until the user types ``expected`` verbatim."""
    sys.stderr.write(
        f"[LIVE] Type exactly {expected!r} to proceed (anything else aborts):\n> "
    )
    sys.stderr.flush()
    try:
        answer = input().strip()
    except EOFError:
        return False
    return answer == expected


def _backup_djay_db(db_path: Path, backup_dir: Path) -> tuple[Path, str]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    dst = backup_dir / f"djay_MediaLibrary.{ts}.db"
    shutil.copy2(db_path, dst)
    if dst.stat().st_size <= 0:
        raise PlaylistApplyError(f"Backup failed: {dst} has size 0")
    return dst, ts


_REVERSAL_TEMPLATE = '''\
"""Auto-generated reversal for apps.sync.playlist_apply run __TS__.

Run this script while djay is quit to restore the pre-apply state.

CloudKit warning: __WARNING__
"""
from __future__ import annotations

import shutil
from pathlib import Path

BACKUP = Path(r"__BACKUP__")
TARGET = Path(r"__TARGET__")


def main() -> int:
    if not BACKUP.exists():
        print(f"backup missing: {BACKUP}")
        return 2
    print(f"restoring {BACKUP} -> {TARGET}")
    shutil.copy2(BACKUP, TARGET)
    print("done. Keep djay quit until iCloud finishes settling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _write_reversal_script(
    backup_path: Path,
    target_path: Path,
    backup_dir: Path,
    ts: str,
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    out = backup_dir / f"restore-playlist-sync-{ts}.py"
    body = (
        _REVERSAL_TEMPLATE.replace("__TS__", ts)
        .replace("__BACKUP__", str(backup_path))
        .replace("__TARGET__", str(target_path))
        .replace("__WARNING__", CLOUDKIT_WARNING)
    )
    out.write_text(body, encoding="utf-8")
    return out


# ----- UUID <-> rowid resolver -----------------------------------------


def resolve_userdata_rowids(
    con: sqlite3.Connection, track_uuids: list[str]
) -> list[int]:
    """Return ``database2.rowid`` for each ``mediaItemUserData.key``, in input order.

    Raises :class:`PlaylistApplyError` listing the missing UUIDs when any
    track has no ``mediaItemUserData`` row.
    """
    if not track_uuids:
        return []
    placeholders = ",".join("?" * len(track_uuids))
    cur = con.execute(
        "SELECT rowid, key FROM database2 "
        f"WHERE collection = 'mediaItemUserData' AND key IN ({placeholders})",
        list(track_uuids),
    )
    by_key: dict[str, int] = {key: rowid for rowid, key in cur.fetchall()}
    missing = [u for u in track_uuids if u not in by_key]
    if missing:
        raise PlaylistApplyError(
            f"mediaItemUserData rows not found for {len(missing)} uuid(s): "
            + ", ".join(missing[:3])
            + (" ..." if len(missing) > 3 else "")
        )
    return [by_key[u] for u in track_uuids]


# ----- Plan loader ------------------------------------------------------


def _load_plan(path: Path) -> dict:
    if not path.exists():
        raise PlaylistApplyError(
            f"plan file not found at {path}. Run "
            "`python -m apps.sync.playlist_diff` first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ----- Apply result -----------------------------------------------------


@dataclass(slots=True)
class PlaylistOpResult:
    rb_id: str
    rb_name: str
    op: str
    djay_uuid: str | None
    status: str  # "written" | "verified" | "skipped" | "failed"
    message: str = ""


@dataclass(slots=True)
class ApplyResult:
    backup_path: Path | None = None
    reversal_script: Path | None = None
    per_playlist: list[PlaylistOpResult] = field(default_factory=list)

    @property
    def all_verified(self) -> bool:
        """True when every op ended verified or (no-op) skipped."""
        return all(r.status in ("verified", "skipped") for r in self.per_playlist)


# ----- Writer -----------------------------------------------------------


def _apply_single_op(
    con: sqlite3.Connection, op: dict, *, leaf_type_byte: int | None
) -> PlaylistOpResult:
    rb_id = op.get("rb_id", "")
    rb_name = op.get("rb_name", "")
    op_kind = op.get("op", "noop")
    djay_uuid = op.get("djay_uuid")

    if op_kind == "noop":
        return PlaylistOpResult(
            rb_id=rb_id, rb_name=rb_name, op=op_kind, djay_uuid=djay_uuid,
            status="skipped", message="already_in_sync",
        )

    target = [m["djay_uuid"] for m in op.get("target_members", [])]
    try:
        rowids = resolve_userdata_rowids(con, target)
    except PlaylistApplyError as exc:
        return PlaylistOpResult(
            rb_id=rb_id, rb_name=rb_name, op=op_kind, djay_uuid=djay_uuid,
            status="failed", message=str(exc),
        )

    if op_kind == "create":
        djay_uuid = _uuid.uuid4().hex
        blob = ptsaf.build_playlist_blob(
            djay_uuid, rb_name, kind="leaf", leaf_type_byte=leaf_type_byte
        )
        con.execute(
            "INSERT INTO database2 (collection, key, data) VALUES (?, ?, ?)",
            ("mediaItemPlaylists", djay_uuid, blob),
        )

    page_key = ptsaf.new_page_key()
    page_data = ptsaf.build_page_data(rowids)
    con.execute(
        'DELETE FROM view_mediaItemPlaylistView_page WHERE "group" = ?',
        (djay_uuid,),
    )
    con.execute(
        'INSERT INTO view_mediaItemPlaylistView_page '
        '(pageKey, "group", prevPageKey, count, data) '
        "VALUES (?, ?, NULL, ?, ?)",
        (page_key, djay_uuid, len(rowids), page_data),
    )

    return PlaylistOpResult(
        rb_id=rb_id, rb_name=rb_name, op=op_kind, djay_uuid=djay_uuid,
        status="written", message=f"{len(rowids)} members",
    )


def _verify_playlist_members(
    con: sqlite3.Connection, djay_uuid: str, expected_uuids: list[str]
) -> tuple[bool, str]:
    rows = con.execute(
        'SELECT data FROM view_mediaItemPlaylistView_page WHERE "group" = ?',
        (djay_uuid,),
    ).fetchall()
    rowids: list[int] = []
    for (blob,) in rows:
        rowids.extend(ptsaf.parse_page_data(blob))

    if not rowids and not expected_uuids:
        return True, "empty"

    mapping = {
        int(rowid): key
        for rowid, key in con.execute(
            "SELECT rowid, key FROM database2 WHERE collection = 'mediaItemUserData'"
        )
    }
    actual = [mapping.get(r, "") for r in rowids]
    if actual == expected_uuids:
        return True, f"{len(actual)} members ok"
    return False, f"expected {expected_uuids!r}, got {actual!r}"


def apply_plan(
    plan: dict,
    *,
    db_path: Path,
    playlist_filter: set[str] | None = None,
    leaf_type_byte: int | None = None,
) -> ApplyResult:
    """Apply the plan against ``db_path``. One transaction per op."""
    result = ApplyResult()
    con = sqlite3.connect(
        f"file:{db_path}?mode=rwc", uri=True, isolation_level=None
    )
    try:
        for op in plan.get("playlists", []):
            if op.get("op") == "noop":
                result.per_playlist.append(
                    PlaylistOpResult(
                        rb_id=op.get("rb_id", ""),
                        rb_name=op.get("rb_name", ""),
                        op="noop",
                        djay_uuid=op.get("djay_uuid"),
                        status="skipped",
                        message="already_in_sync",
                    )
                )
                continue
            if playlist_filter is not None and op.get("rb_name", "") not in playlist_filter:
                continue

            con.execute("BEGIN IMMEDIATE")
            try:
                op_result = _apply_single_op(
                    con, op, leaf_type_byte=leaf_type_byte
                )
                if op_result.status == "failed":
                    con.execute("ROLLBACK")
                    result.per_playlist.append(op_result)
                    continue
                con.execute("COMMIT")
            except Exception as exc:  # noqa: BLE001
                try:
                    con.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                result.per_playlist.append(
                    PlaylistOpResult(
                        rb_id=op.get("rb_id", ""),
                        rb_name=op.get("rb_name", ""),
                        op=op.get("op", "?"),
                        djay_uuid=op.get("djay_uuid"),
                        status="failed",
                        message=f"transaction aborted: {exc}",
                    )
                )
                continue

            ok, msg = _verify_playlist_members(
                con,
                op_result.djay_uuid or "",
                [m["djay_uuid"] for m in op.get("target_members", [])],
            )
            op_result.status = "verified" if ok else "failed"
            op_result.message = msg if ok else f"readback mismatch: {msg}"
            result.per_playlist.append(op_result)
    finally:
        con.close()

    return result


# ----- CLI --------------------------------------------------------------


def _dry_run_print(plan: dict, filter_: set[str] | None) -> None:
    try:
        from rich.console import Console
        from rich.table import Table

        console = Console(width=120)
        totals = {"create": 0, "update": 0, "noop": 0}
        for op in plan.get("playlists", []):
            totals[op.get("op", "noop")] = totals.get(op.get("op", "noop"), 0) + 1
        table = Table(title="Playlist sync (dry-run simulation)")
        table.add_column("rb_name")
        table.add_column("op")
        table.add_column("target", justify="right")
        table.add_column("adds", justify="right")
        table.add_column("removes", justify="right")
        for op in plan.get("playlists", []):
            if filter_ is not None and op.get("rb_name") not in filter_:
                continue
            table.add_row(
                op.get("rb_name", ""),
                op.get("op", ""),
                str(len(op.get("target_members", []))),
                str(len(op.get("adds", []))),
                str(len(op.get("removes", []))),
            )
        console.print(table)
        console.print(
            f"[bold]Totals[/bold]: create={totals.get('create', 0)} "
            f"update={totals.get('update', 0)} noop={totals.get('noop', 0)}"
        )
    except ImportError:  # pragma: no cover
        for op in plan.get("playlists", []):
            print(op.get("op"), op.get("rb_name"))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="apps.sync.playlist_apply",
        description=(
            "Apply the RB-canonical playlist diff into djay's MediaLibrary.db "
            "under the Phase 1 six-rail safety pattern."
        ),
    )
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--live", action="store_true", default=False)
    parser.add_argument("--i-understand-the-risks", action="store_true", default=False)
    parser.add_argument(
        "--playlists",
        default=None,
        help="comma-separated canonical playlist names to apply (cautious mode)",
    )
    parser.add_argument(
        "--bulk",
        action="store_true",
        help="apply every non-noop op in the plan (mutex with --playlists)",
    )
    parser.add_argument(
        "--leaf-type-byte",
        type=lambda v: int(v, 0),
        default=None,
        help=(
            "override the leaf playlist TSAF type enum byte. Use once you "
            "have captured the byte from a live djay fixture."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.live and not args.i_understand_the_risks:
        sys.stderr.write("--live requires --i-understand-the-risks\n")
        return 2
    if args.playlists and args.bulk:
        sys.stderr.write("--playlists and --bulk are mutually exclusive\n")
        return 2

    try:
        plan = _load_plan(args.plan)
    except PlaylistApplyError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    filter_: set[str] | None = None
    if args.playlists:
        filter_ = {s.strip() for s in args.playlists.split(",") if s.strip()}

    if not args.live:
        _dry_run_print(plan, filter_)
        return 0

    try:
        _assert_djay_quit()
        _assert_rekordbox_quit()
    except PlaylistApplyError as exc:
        sys.stderr.write(f"abort: {exc}\n")
        return 3

    if not _typed_confirm():
        sys.stderr.write("typed confirmation failed; aborting\n")
        return 3

    db_path = _live_db_path(True)
    if not db_path.exists():
        sys.stderr.write(f"live djay DB missing: {db_path}\n")
        return 4

    backup_path, ts = _backup_djay_db(db_path, args.backup_dir)
    sys.stderr.write(f"[ok] backup -> {backup_path}\n")

    effective_filter = None if args.bulk else filter_

    try:
        result = apply_plan(
            plan,
            db_path=db_path,
            playlist_filter=effective_filter,
            leaf_type_byte=args.leaf_type_byte,
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"apply raised: {exc}\n")
        sys.stderr.write(f"restore with:  cp {backup_path} {db_path}\n")
        return 5

    rev = _write_reversal_script(backup_path, db_path, args.backup_dir, ts)
    result.backup_path = backup_path
    result.reversal_script = rev

    sys.stderr.write(f"[ok] reversal -> {rev}\n")
    sys.stderr.write(f"[warning] {CLOUDKIT_WARNING}\n")
    for r in result.per_playlist:
        sys.stderr.write(f"  {r.status:9s} {r.op:6s} {r.rb_name}  {r.message}\n")
    return 0 if result.all_verified else 6


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
