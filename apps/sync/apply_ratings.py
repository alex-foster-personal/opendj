"""Phase 4 SYNC-06 bulk ratings sync CLI."""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from apps.shared import paths
from apps.shared.rekordbox_writeback import require_writeback_enabled
from apps.sync.safety import LiveWriteSession, SafetyAbort

# ----- Path resolution (mirrors playlist_apply._live_db_path) -----------


def _live_rb_db_path(live: bool) -> Path:
    """Return the Rekordbox DB path we will open.

    ``--live`` MUST resolve to :data:`paths.REKORDBOX_LIVE_DB`; otherwise
    we open the working copy under ``data/``.
    """
    if live:
        require_writeback_enabled("module.sync.apply_ratings")
        return paths.REKORDBOX_LIVE_DB
    return paths.REKORDBOX_WORKING_DB


def _live_djay_db_path(live: bool) -> Path:
    """Return the djay DB path we will open.

    ``--live`` MUST resolve to :data:`paths.DJAY_LIVE_DB`; otherwise we
    open the working copy under ``data/``.
    """
    return paths.DJAY_LIVE_DB if live else paths.DJAY_WORKING_DB


def _load_ratings_diff(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


def _summarise_plan(rows: list[dict]) -> dict[str, int]:
    counts = Counter(r.get("resolution", "") for r in rows)
    return dict(counts)


def dry_run(rows: list[dict]) -> int:
    if not rows:
        print("[apply_ratings] no rows in ratings-diff.csv (dry-run).")
        return 0
    summary = _summarise_plan(rows)
    print("[apply_ratings] dry-run summary (resolution -> count):")
    for k, v in sorted(summary.items()):
        print(f"  {k}: {v}")
    return 0


def _write_rb_rating(db, content_id: str, rating: int) -> bool:
    try:
        content = db.get_content(ID=str(content_id)).one()
    except Exception:
        return False
    content.Rating = int(rating)
    db.commit()
    try:
        again = db.get_content(ID=str(content_id)).one()
        return int(getattr(again, "Rating", -1)) == int(rating)
    except Exception:
        return False


def _verify_rb_rating(db, content_id: str, expected: int) -> bool:
    """Rail 4 (post-write verify): re-read the Rating and compare.

    Returns ``True`` iff the row exists and its ``Rating`` matches.
    A write session that receives a ``False`` here will prompt the
    user to abort/skip/continue (non-tty defaults to abort, matching
    :class:`apps.sync.safety.LiveWriteSession`).
    """
    try:
        again = db.get_content(ID=str(content_id)).one()
    except Exception:
        return False
    return int(getattr(again, "Rating", -1)) == int(expected)


def _write_djay_rating(db_path: Path, uuid: str, rating: int) -> bool:
    import sqlite3

    from apps.shared.djay_db import extract_rating_from_tsaf
    from apps.sync.djay_writer import patch_rating

    with sqlite3.connect(str(db_path)) as con:
        row = con.execute(
            "SELECT data FROM database2 WHERE collection = 'mediaItemUserData' "
            "AND key = ?",
            (uuid,),
        ).fetchone()
        if not row:
            return False
        blob = row[0] or b""
        new_blob = patch_rating(blob, rating)
        con.execute(
            "UPDATE database2 SET data = ? "
            "WHERE collection = 'mediaItemUserData' AND key = ?",
            (new_blob, uuid),
        )
        con.commit()
        again = con.execute(
            "SELECT data FROM database2 WHERE collection = 'mediaItemUserData' "
            "AND key = ?",
            (uuid,),
        ).fetchone()
    return bool(again) and extract_rating_from_tsaf(again[0]) == int(rating)


def _verify_djay_rating(db_path: Path, uuid: str, expected: int) -> bool:
    """Rail 4 (post-write verify): re-read the TSAF rating and compare.

    Opens the DB read-only and checks that the patched ``Rating`` byte
    round-trips through :func:`extract_rating_from_tsaf`.
    """
    import sqlite3

    from apps.shared.djay_db import extract_rating_from_tsaf

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            row = con.execute(
                "SELECT data FROM database2 "
                "WHERE collection = 'mediaItemUserData' AND key = ?",
                (uuid,),
            ).fetchone()
    except Exception:
        return False
    if not row:
        return False
    return extract_rating_from_tsaf(row[0]) == int(expected)


def live_run(
    rows: list[dict],
    *,
    rb_db_path: Path,
    djay_db_path: Path,
    only_tracks: set[str] | None = None,
    flag_ok: bool = False,
    prefer: str = "newest",  # noqa: ARG001 - retained for keyword callers
) -> int:
    # Note: rb_db_path and djay_db_path are REQUIRED (no defaults). The
    # previous signature defaulted to the WORKING-copy paths, which meant a
    # caller that forgot to route --live through _live_rb_db_path /
    # _live_djay_db_path would silently operate on the working copy even
    # when the user asked for --live. That was issue #1 (Phase 2 [I1]).
    # Keeping these required turns the footgun into an immediate TypeError.
    targets_rb: list[tuple[str, int]] = []
    targets_djay: list[tuple[str, int]] = []

    for row in rows:
        rb_id = row.get("rb_content_id", "")
        djay_uuid = row.get("djay_uuid", "")
        if only_tracks is not None and djay_uuid not in only_tracks and rb_id not in only_tracks:
            continue
        res = row.get("resolution", "")
        try:
            rb_val = int(row.get("rb_value") or 0)
        except ValueError:
            rb_val = 0
        try:
            djay_val = int(row.get("djay_value") or 0)
        except ValueError:
            djay_val = 0
        if res == "accept_rb" and djay_uuid:
            targets_djay.append((djay_uuid, rb_val))
        elif res == "accept_djay" and rb_id:
            targets_rb.append((rb_id, djay_val))

    written_rb = 0
    written_djay = 0

    if targets_rb:
        from apps.shared.rekordbox_db import open_db

        db = open_db(rb_db_path)
        try:
            # Rail 4: post-write verify is routed through the session's
            # verifier callback. Mapping per-track track_id -> expected
            # Rating so the callback can readback on demand.
            rb_expected: dict[str, int] = {
                str(cid): int(rating) for cid, rating in targets_rb
            }

            def rb_verifier(track_id: str, _unused: object = None) -> bool:
                return _verify_rb_rating(
                    db, track_id, rb_expected[str(track_id)],
                )

            with LiveWriteSession(
                target="rekordbox",
                reason="SYNC-06 bulk ratings (RB side)",
                flag_ok=flag_ok,
                db_path=rb_db_path,
                verifier=rb_verifier,
            ) as sess:
                for content_id, rating in targets_rb:
                    with sess.per_track(content_id) as w:
                        ok = _write_rb_rating(db, content_id, rating)
                        w.write(rating)
                        if ok and w.verify_readback():
                            w.append_reverse(
                                f"# revert RB rating for ContentID={content_id}"
                            )
                            written_rb += 1
        finally:
            try:
                db.close()
            except Exception:
                pass

    if targets_djay:
        djay_expected: dict[str, int] = {
            str(uuid): int(rating) for uuid, rating in targets_djay
        }

        def djay_verifier(track_id: str, _unused: object = None) -> bool:
            return _verify_djay_rating(
                djay_db_path, track_id, djay_expected[str(track_id)],
            )

        with LiveWriteSession(
            target="djay",
            reason="SYNC-06 bulk ratings (djay side)",
            flag_ok=flag_ok,
            db_path=djay_db_path,
            verifier=djay_verifier,
        ) as sess:
            for uuid, rating in targets_djay:
                with sess.per_track(uuid) as w:
                    ok = _write_djay_rating(djay_db_path, uuid, rating)
                    w.write(rating)
                    if ok and w.verify_readback():
                        w.append_reverse(
                            f"# revert djay rating for uuid={uuid}"
                        )
                        written_djay += 1

    print(
        f"[apply_ratings] wrote rb={written_rb} djay={written_djay} "
        f"of {len(targets_rb) + len(targets_djay)} planned"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apps.sync.apply_ratings")
    parser.add_argument(
        "--diff-csv",
        type=Path,
        default=paths.DATA_DIR / "sync" / "ratings-diff.csv",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--bulk", action="store_true")
    parser.add_argument("--tracks", type=str, default="")
    parser.add_argument(
        "--i-understand-the-risks",
        dest="i_understand_the_risks",
        action="store_true",
    )
    parser.add_argument("--prefer", choices=["rb", "djay", "newest"], default="newest")
    parser.add_argument(
        "--skip-cautious-check",
        action="store_true",
        help=(
            "P04-03: skip the dry-run->cautious->bulk sequencing check. "
            "Only use after documenting why the cautious stage was skipped."
        ),
    )
    args = parser.parse_args(argv)

    rows = _load_ratings_diff(args.diff_csv)
    if not args.live:
        return dry_run(rows)
    if args.bulk and not args.i_understand_the_risks:
        print("[apply_ratings] --bulk requires --i-understand-the-risks", file=sys.stderr)
        return 2

    only_tracks: set[str] | None = None
    if args.tracks:
        only_tracks = {t.strip() for t in args.tracks.split(",") if t.strip()}

    # P04-03: enforce dry-run -> cautious -> bulk. Cautious runs are
    # identified by --tracks=... (a non-empty filter); bulk runs require a
    # prior cautious success stamp.
    from apps.sync.safety import (
        mark_cautious_success,
        require_cautious_before_bulk,
    )
    if args.bulk:
        try:
            require_cautious_before_bulk(
                "apply_ratings", override=args.skip_cautious_check
            )
        except SafetyAbort as e:
            print(f"[apply_ratings] SafetyAbort: {e}", file=sys.stderr)
            return 3

    try:
        rc = live_run(
            rows,
            only_tracks=only_tracks,
            flag_ok=args.i_understand_the_risks,
            prefer=args.prefer,
            rb_db_path=_live_rb_db_path(args.live),
            djay_db_path=_live_djay_db_path(args.live),
        )
    except SafetyAbort as e:
        print(f"[apply_ratings] SafetyAbort: {e}", file=sys.stderr)
        return 3

    if rc == 0 and only_tracks and not args.bulk:
        mark_cautious_success("apply_ratings")
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["dry_run", "live_run", "main"]
