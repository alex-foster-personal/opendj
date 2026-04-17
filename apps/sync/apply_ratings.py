"""Phase 4 SYNC-06 bulk ratings sync CLI."""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from apps.shared import paths
from apps.sync.safety import LiveWriteSession, SafetyAbort


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


def live_run(
    rows: list[dict],
    *,
    only_tracks: set[str] | None = None,
    flag_ok: bool = False,
    prefer: str = "newest",
    rb_db_path: Path = paths.REKORDBOX_WORKING_DB,
    djay_db_path: Path = paths.DJAY_WORKING_DB,
) -> int:
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
            with LiveWriteSession(
                target="rekordbox",
                reason="SYNC-06 bulk ratings (RB side)",
                flag_ok=flag_ok,
                db_path=rb_db_path,
            ) as sess:
                for content_id, rating in targets_rb:
                    with sess.per_track(content_id) as w:
                        ok = _write_rb_rating(db, content_id, rating)
                        w.write(rating)
                        if ok:
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
        with LiveWriteSession(
            target="djay",
            reason="SYNC-06 bulk ratings (djay side)",
            flag_ok=flag_ok,
            db_path=djay_db_path,
        ) as sess:
            for uuid, rating in targets_djay:
                with sess.per_track(uuid) as w:
                    ok = _write_djay_rating(djay_db_path, uuid, rating)
                    w.write(rating)
                    if ok:
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
    try:
        return live_run(
            rows,
            only_tracks=only_tracks,
            flag_ok=args.i_understand_the_risks,
            prefer=args.prefer,
        )
    except SafetyAbort as e:
        print(f"[apply_ratings] SafetyAbort: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["dry_run", "live_run", "main"]
