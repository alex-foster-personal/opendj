"""Phase 4 SYNC-05 analysis-field bulk sync CLI."""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from apps.analysis.selection import SelectionError
from apps.shared import paths
from apps.shared.rekordbox_writeback import require_writeback_enabled
from apps.sync.analysis_csv_live import live_run
from apps.sync.analysis_writeback_diff import (
    WRITEBACK_FIELDS,
    WRITEBACK_LANES,
    UnpromotedLaneError,
)
from apps.sync.safety import SafetyAbort

# Codex P04-02: RB-side writes are only implemented for these fields.
# ``apply_analysis`` used to silently skip any other field and still
# exit 0, which made the CLI report success after a partial apply.
# See ``_write_rb_field`` and ``live_run`` below: unsupported RB fields
# now fail the run with a non-zero exit instead of being dropped.
_SUPPORTED_RB_WRITE_FIELDS = frozenset({
    "bpm", "energy", "key", "loudness_lufs", "loudness_dbtp", "pqtz",
})


# ----- Path resolution (mirrors apply_ratings._live_*_db_path) ----------
#
# v1.0 adversarial review (#1, CRITICAL): prior ``main`` passed no
# ``rb_db_path`` / ``djay_db_path`` into ``live_run`` and so silently
# routed every ``--live`` write into the WORKING DB copies under
# ``data/`` -- never the user's real library. Fix mirrors
# ``apps.sync.apply_ratings``: helpers below resolve the correct path
# per ``--live``, and ``live_run`` now REQUIRES the caller to pass
# them (no more WORKING defaults silently hiding a missing route).


def _live_rb_db_path(live: bool) -> Path:
    """Return the Rekordbox DB path we will open.

    ``--live`` MUST resolve to :data:`paths.REKORDBOX_LIVE_DB`; otherwise
    we open the working copy under ``data/``.
    """
    if live:
        require_writeback_enabled("module.sync.apply_analysis")
        return paths.REKORDBOX_LIVE_DB
    return paths.REKORDBOX_WORKING_DB


def _live_djay_db_path(live: bool) -> Path:
    """Return the djay DB path we will open.

    ``--live`` MUST resolve to :data:`paths.DJAY_LIVE_DB`; otherwise we
    open the working copy under ``data/``.
    """
    return paths.DJAY_LIVE_DB if live else paths.DJAY_WORKING_DB


def _load_diff(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


def _csv_resolution_summary(rows: list[dict]) -> int:
    if not rows:
        print("[apply_analysis] no rows in analysis-diff.csv (dry-run).")
        return 0
    from collections import defaultdict

    per_field: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        per_field[r.get("field", "")][r.get("resolution", "")] += 1
    print("[apply_analysis] dry-run summary:")
    for field in sorted(per_field):
        summary = ", ".join(f"{k}={v}" for k, v in sorted(per_field[field].items()))
        print(f"  {field}: {summary}")
    return 0


def dry_run(
    *,
    state_db: Path | None = None,
    rb_db: Path | None = None,
    lanes: tuple[str, ...] = WRITEBACK_LANES,
    fields: tuple[str, ...] | None = None,
    only_tracks: set[str] | None = None,
) -> int:
    """Write-back dry-run planner (read-only rekordbox + state)."""
    from apps.sync.analysis_writeback import dry_run_writeback

    resolved_state = state_db or paths.STATE_DB
    resolved_rb = rb_db or paths.REKORDBOX_PLAIN_DB
    return dry_run_writeback(
        state_db=resolved_state,
        rb_db=resolved_rb,
        lanes=lanes,
        fields=fields or tuple(WRITEBACK_FIELDS),
        only_tracks=only_tracks,
    )


def _open_rb_connection(path: Path):
    import sqlite3

    try:
        conn = sqlite3.connect(str(path))
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn, conn
    except sqlite3.DatabaseError:
        from apps.shared.rekordbox_db import open_db

        db = open_db(path)
        return db, db.engine.raw_connection()


def _close_rb_connection(handle, conn) -> None:
    try:
        conn.close()
    except Exception:
        pass
    if handle is not conn:
        try:
            handle.close()
        except Exception:
            pass


def _run_writeback_live(
    *,
    state_db: Path,
    lanes: tuple[str, ...],
    fields: tuple[str, ...],
    only_tracks: set[str] | None,
    flag_ok: bool,
) -> int:
    from apps.sync.analysis_writeback import (
        build_writeback_plan,
        live_writeback,
        validate_writeback_plan,
    )

    plan = build_writeback_plan(
        state_db=state_db,
        rb_db_path=paths.REKORDBOX_PLAIN_DB,
        lanes=lanes,
        fields=fields,
        only_tracks=only_tracks,
    )
    validate_writeback_plan(plan)
    rb_db_path = _live_rb_db_path(True)
    handle, conn = _open_rb_connection(rb_db_path)
    try:
        return live_writeback(
            plan=plan,
            state_db=state_db,
            rb_db_path=rb_db_path,
            rb_conn=conn,
            fields=fields,
            flag_ok=flag_ok,
        )
    finally:
        _close_rb_connection(handle, conn)


def _run_writeback_undo(preimage_path: Path) -> int:
    from apps.sync.analysis_writeback import run_undo

    rb_db_path = _live_rb_db_path(True)
    handle, conn = _open_rb_connection(rb_db_path)
    try:
        return run_undo(preimage_path, rb_db_path=rb_db_path, rb_conn=conn)
    finally:
        _close_rb_connection(handle, conn)


class UnsupportedRbFieldError(RuntimeError):
    """RB-side write scheduled for a field we can't safely write.

    Codex P04-02: ``_write_rb_field`` previously returned ``False`` for
    ``manual_bpm`` / ``key_camelot`` / ``tags`` and the CLI still exited
    ``0``. This exception is how we now refuse unsupported fields
    loudly; ``main()`` converts it to a non-zero exit.
    """


def _write_rb_field(db, content_id: str, field: str, value) -> bool:
    if field not in _SUPPORTED_RB_WRITE_FIELDS:
        # Refuse rather than silently skip. See P04-02 note above.
        raise UnsupportedRbFieldError(
            f"RB-side write for field {field!r} is not implemented; "
            "refusing to partial-apply. Supported: "
            f"{sorted(_SUPPORTED_RB_WRITE_FIELDS)}."
        )
    import sqlite3

    if isinstance(db, sqlite3.Connection) and field == "pqtz":
        from apps.sync.analysis_writeback_pqtz import (
            resolve_analysis_dat_path,
            write_pqtz,
        )

        dat_path = resolve_analysis_dat_path(db, content_id)
        if dat_path is None:
            return False
        ok = write_pqtz(dat_path, value)
        if ok:
            db.commit()
        return ok
    if isinstance(db, sqlite3.Connection) and field in (
        "key", "loudness_lufs", "loudness_dbtp", "bpm",
    ):
        from apps.sync.analysis_writeback import write_scalar

        preimage: dict[str, object] = {"created_key_id": None}
        ok = write_scalar(db, content_id, field, value, preimage)
        if ok:
            db.commit()
        return ok
    if field == "pqtz":
        return False
    try:
        content = db.get_content(ID=str(content_id)).one()
    except Exception:
        return False
    if field == "bpm":
        try:
            content.BPM = round(float(value) * 100)
        except (TypeError, ValueError):
            return False
    elif field == "energy":
        try:
            content.ColorID = int(value)
        except (TypeError, ValueError):
            return False
    db.commit()
    return True


def _verify_rb_field(db, content_id: str, field: str, value) -> bool:
    """Rail 4 (post-write verify) for the RB side.

    Reads the row back and checks the just-written column matches the
    integer representation we just stored. ``True`` means the write
    persisted exactly as intended; ``False`` trips the session's
    verify-failure branch (abort/skip/continue).
    """
    import sqlite3

    if isinstance(db, sqlite3.Connection) and field == "pqtz":
        from apps.sync.analysis_writeback_pqtz import (
            resolve_analysis_dat_path,
            verify_pqtz,
        )

        dat_path = resolve_analysis_dat_path(db, content_id)
        if dat_path is None:
            return False
        return verify_pqtz(dat_path, value)
    if isinstance(db, sqlite3.Connection) and field in (
        "key", "loudness_lufs", "loudness_dbtp", "bpm",
    ):
        from apps.sync.analysis_writeback import verify_scalar

        return verify_scalar(db, content_id, field, value)
    if field == "pqtz":
        return False
    try:
        content = db.get_content(ID=str(content_id)).one()
    except Exception:
        return False
    try:
        if field == "bpm":
            return int(getattr(content, "BPM", -1)) == round(float(value) * 100)
        if field == "energy":
            return int(getattr(content, "ColorID", -1)) == int(value)
    except (TypeError, ValueError):
        return False
    return False


_LIVE_DEFAULT_FIELDS = "bpm,manual_bpm,key_camelot,energy,tags"
_WRITEBACK_DEFAULT_FIELDS = "bpm,key,loudness_lufs,loudness_dbtp,pqtz"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apps.sync.apply_analysis")
    parser.add_argument(
        "--diff-csv",
        type=Path,
        default=None,
        help="SYNC-05 rb-vs-djay CSV resolution summary (not the no-flag default).",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--bulk", action="store_true")
    parser.add_argument("--tracks", type=str, default="")
    parser.add_argument(
        "--fields",
        type=str,
        default="",
        help="Comma-separated fields; default depends on --live.",
    )
    parser.add_argument(
        "--lanes",
        type=str,
        default=",".join(WRITEBACK_LANES),
        help="Write-back lanes (dry-run and write-back live; ignored with --diff-csv --live).",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=paths.STATE_DB,
        help="State DB (write-back dry-run and write-back live; ignored with --diff-csv --live).",
    )
    parser.add_argument(
        "--rb-db",
        type=Path,
        default=paths.REKORDBOX_PLAIN_DB,
        help="Rekordbox plain working copy for write-back dry-run only.",
    )
    parser.add_argument(
        "--undo",
        type=Path,
        default=None,
        help="Restore analysis preimages from a prior write-back live session.",
    )
    parser.add_argument(
        "--i-understand-the-risks",
        dest="i_understand_the_risks",
        action="store_true",
    )
    parser.add_argument(
        "--skip-cautious-check",
        action="store_true",
        help=(
            "P04-03: skip the dry-run->cautious->bulk sequencing check. "
            "Only use after documenting why the cautious stage was skipped."
        ),
    )
    args = parser.parse_args(argv)

    if args.undo is not None:
        if args.live:
            print("[apply_analysis] --undo is mutually exclusive with --live", file=sys.stderr)
            return 2
        try:
            return _run_writeback_undo(args.undo)
        except SafetyAbort as e:
            print(f"[apply_analysis] SafetyAbort: {e}", file=sys.stderr)
            return 3

    fields_raw = args.fields.strip()
    if fields_raw:
        fields_list = tuple(f.strip() for f in fields_raw.split(",") if f.strip())
    elif args.live and args.diff_csv is not None:
        fields_list = tuple(
            f.strip() for f in _LIVE_DEFAULT_FIELDS.split(",") if f.strip()
        )
    else:
        fields_list = tuple(
            f.strip() for f in _WRITEBACK_DEFAULT_FIELDS.split(",") if f.strip()
        )

    only_tracks: set[str] | None = None
    if args.tracks:
        only_tracks = {t.strip() for t in args.tracks.split(",") if t.strip()}

    lanes = tuple(l.strip() for l in args.lanes.split(",") if l.strip())

    if not args.live:
        if args.diff_csv is not None:
            return _csv_resolution_summary(_load_diff(args.diff_csv))
        try:
            return dry_run(
                state_db=args.state_db,
                rb_db=args.rb_db,
                lanes=lanes,
                fields=fields_list,
                only_tracks=only_tracks,
            )
        except UnpromotedLaneError as e:
            print(f"[apply_analysis] {e}", file=sys.stderr)
            return 2
        except SelectionError as e:
            print(f"[apply_analysis] {e}", file=sys.stderr)
            return 2
        except FileNotFoundError as e:
            print(f"[apply_analysis] {e}", file=sys.stderr)
            return 2

    if args.diff_csv is None:
        try:
            return _run_writeback_live(
                state_db=args.state_db,
                lanes=lanes,
                fields=fields_list,
                only_tracks=only_tracks,
                flag_ok=args.i_understand_the_risks,
            )
        except UnpromotedLaneError as e:
            print(f"[apply_analysis] {e}", file=sys.stderr)
            return 2
        except SelectionError as e:
            print(f"[apply_analysis] {e}", file=sys.stderr)
            return 2
        except FileNotFoundError as e:
            print(f"[apply_analysis] {e}", file=sys.stderr)
            return 2
        except SafetyAbort as e:
            print(f"[apply_analysis] SafetyAbort: {e}", file=sys.stderr)
            return 3
        except UnsupportedRbFieldError as e:
            print(f"[apply_analysis] UnsupportedRbField: {e}", file=sys.stderr)
            return 4

    csv_path = args.diff_csv
    rows = _load_diff(csv_path)
    if args.bulk and not args.i_understand_the_risks:
        print("[apply_analysis] --bulk requires --i-understand-the-risks", file=sys.stderr)
        return 2

    fields = set(fields_list)

    from apps.sync.safety import (
        mark_cautious_success,
        require_cautious_before_bulk,
    )
    if args.bulk:
        try:
            require_cautious_before_bulk(
                "apply_analysis", override=args.skip_cautious_check
            )
        except SafetyAbort as e:
            print(f"[apply_analysis] SafetyAbort: {e}", file=sys.stderr)
            return 3

    from apps.shared.rekordbox_db import open_db

    try:
        rc = live_run(
            rows,
            fields=fields,
            only_tracks=only_tracks,
            flag_ok=args.i_understand_the_risks,
            rb_db_path=_live_rb_db_path(args.live),
            djay_db_path=_live_djay_db_path(args.live),
            open_rb_db=open_db,
        )
    except SafetyAbort as e:
        print(f"[apply_analysis] SafetyAbort: {e}", file=sys.stderr)
        return 3
    except UnsupportedRbFieldError as e:
        print(f"[apply_analysis] UnsupportedRbField: {e}", file=sys.stderr)
        return 4

    if rc == 0 and only_tracks and not args.bulk:
        mark_cautious_success("apply_analysis")
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["UnpromotedLaneError", "dry_run", "live_run", "main"]
