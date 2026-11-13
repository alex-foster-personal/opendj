"""Phase 4 SYNC-06 bulk ratings sync CLI."""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections import Counter
from pathlib import Path

from apps.shared import paths
from apps.shared.rekordbox_writeback import require_writeback_enabled
from apps.sync.diff_csv import load_diff_rows, match_provenance
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
    """Stage one Rekordbox Rating change without committing (batch commits once).

    Returns ``False`` when the content row is absent.
    """
    try:
        content = db.get_content(ID=str(content_id)).one()
    except Exception:
        return False
    content.Rating = int(rating)
    return True


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
    from apps.sync.djay_writer import AmbiguousRatingFieldError, patch_rating

    with sqlite3.connect(str(db_path)) as con:
        row = con.execute(
            "SELECT data FROM database2 WHERE collection = 'mediaItemUserData' "
            "AND key = ?",
            (uuid,),
        ).fetchone()
        if not row:
            return False
        from apps.sync.djay_writer import is_patchable_tsaf_blob

        if not is_patchable_tsaf_blob(row[0]):
            return False  # malformed TSAF blob: report a failed write, leave bytes as-is
        blob = row[0] or b""
        try:
            new_blob = patch_rating(blob, rating)
        except AmbiguousRatingFieldError:
            return False  # ambiguous rating layout: refuse a speculative write
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
    rating = _read_djay_rating(db_path, uuid)
    return rating is not None and rating == int(expected)


def _read_djay_rating(db_path: Path, uuid: str) -> int | None:
    """Return the current djay TSAF rating for ``uuid``, or ``None`` if absent."""
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
        return None
    if not row:
        return None
    return extract_rating_from_tsaf(row[0] or b"")


def restore_djay_rating(db_path: str | Path, uuid: str, prior: int) -> bool:
    """Write ``prior`` back as the djay rating for ``uuid`` and verify it."""
    return _write_djay_rating(Path(db_path), str(uuid), int(prior))


def _djay_reverse_command(db_path: Path, uuid: str, prior: int) -> str:
    """Return the reverse.sh line that restores one djay rating."""
    code = (
        "import sys; from apps.sync.apply_ratings import restore_djay_rating; "
        "sys.exit(0 if restore_djay_rating(sys.argv[1], sys.argv[2], "
        "int(sys.argv[3])) else 1)"
    )
    args = " ".join(shlex.quote(a) for a in (str(db_path), uuid, str(prior)))
    return f"uv run python -c {shlex.quote(code)} {args}"


def replay_djay_rating_reversal(records_path: Path, db_path: Path) -> int:
    """Replay djay rating records from ``reverse.jsonl``; return rows restored."""
    restored = 0
    lines = records_path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("target") != "djay" or rec.get("field") != "rating":
            continue
        if restore_djay_rating(db_path, str(rec["track_id"]), int(rec["prior"])):
            restored += 1
    return restored


def _rating_int(value: object) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _require_rating_domain(value: int, field: str, row_id: str) -> int:
    if not 0 <= value <= 5:
        raise ValueError(
            f"[apply_ratings] {field}={value} for {row_id!r} is outside the 0..5 rating domain"
        )
    return value


def _resolve_with_prefer(res: str, rb_val: int, djay_val: int, prefer: str) -> str:
    """Turn a planned ``conflict`` into a side when ``--prefer`` names one.

    The diff CSV carries no per-side timestamps, so ``newest`` has no
    evidence to act on and a ``conflict`` row stays unwritten. Only rows
    where both sides hold differing non-zero ratings are resolved.
    """
    if res != "conflict" or not rb_val or not djay_val or rb_val == djay_val:
        return res
    if prefer == "rb":
        return "accept_rb"
    if prefer == "djay":
        return "accept_djay"
    return res


def _plan_rating_targets(
    rows: list[dict],
    only_tracks: set[str] | None,
    prefer: str = "newest",
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Plan per-vendor rating writes; refuse contradictory duplicate targets.

    Only rows whose ``field`` is exactly ``rating`` are planned (SYNC-12989).
    A vendor target (RB content ID or djay UUID) planned twice with the same
    rating is written once; planned twice with different ratings raises
    :class:`SafetyAbort` before either database is opened.
    """
    targets_rb: list[tuple[str, int]] = []
    targets_djay: list[tuple[str, int]] = []
    seen_rb: dict[str, int] = {}
    seen_djay: dict[str, int] = {}
    for row in rows:
        # SYNC-12989: only rating rows may drive rating writes, so a row from
        # another sync artifact (or one with no field) can never be applied.
        if row.get("field") != "rating":
            continue
        rb_id = row.get("rb_content_id", "")
        djay_uuid = row.get("djay_uuid", "")
        if only_tracks is not None and djay_uuid not in only_tracks and rb_id not in only_tracks:
            continue
        row_id = djay_uuid or rb_id
        rb_val = _require_rating_domain(_rating_int(row.get("rb_value")), "rb_value", row_id)
        djay_val = _require_rating_domain(
            _rating_int(row.get("djay_value")), "djay_value", row_id
        )
        res = _resolve_with_prefer(row.get("resolution", ""), rb_val, djay_val, prefer)
        if res == "accept_rb" and djay_uuid:
            _add_rating_target(targets_djay, seen_djay, "djay uuid", djay_uuid, rb_val)
        elif res == "accept_djay" and rb_id:
            _add_rating_target(targets_rb, seen_rb, "rekordbox content id", rb_id, djay_val)
    return targets_rb, targets_djay


def _add_rating_target(
    targets: list[tuple[str, int]],
    seen: dict[str, int],
    label: str,
    target_id: str,
    rating: int,
) -> None:
    """Append one target, skipping same-rating repeats and refusing conflicts."""
    key = str(target_id)
    if key in seen:
        if seen[key] != rating:
            raise SafetyAbort(
                f"conflicting duplicate {label} {key} in ratings diff: "
                f"planned ratings {seen[key]} and {rating}"
            )
        return
    seen[key] = rating
    targets.append((key, rating))


def _row_provenance(
    row: dict, only_tracks: set[str] | None, prefer: str = "newest"
) -> dict[str, object]:
    """Describe the write decision for one ratings-diff row and why it was made.

    Mirrors :func:`_plan_rating_targets`: a row whose ``field`` is not
    ``rating`` is never written, and a ``conflict`` row that ``prefer``
    settles is a write from the preferred side (``resolution`` keeps the
    planned ``conflict`` so the record shows the policy made the call).
    """
    rb_id = row.get("rb_content_id", "")
    djay_uuid = row.get("djay_uuid", "")
    res = row.get("resolution", "")
    rb_val = _rating_int(row.get("rb_value"))
    djay_val = _rating_int(row.get("djay_value"))
    record: dict[str, object] = {
        "rb_content_id": rb_id,
        "djay_uuid": djay_uuid,
        "resolution": res,
        "rb_rating": rb_val,
        "djay_rating": djay_val,
        **match_provenance(row),
        "source": None,
        "target": None,
        "source_rating": None,
        "target_rating_before": None,
        "planned_target_rating": None,
    }
    planned = _resolve_with_prefer(res, rb_val, djay_val, prefer)
    if row.get("field") != "rating":
        record["decision"] = "skip"
    elif only_tracks is not None and djay_uuid not in only_tracks and rb_id not in only_tracks:
        record["decision"] = "filtered"
    elif planned == "accept_rb" and djay_uuid:
        record.update(
            decision="write", source="rekordbox", target="djay",
            source_rating=rb_val, target_rating_before=djay_val,
            planned_target_rating=rb_val,
        )
    elif planned == "accept_djay" and rb_id:
        record.update(
            decision="write", source="djay", target="rekordbox",
            source_rating=djay_val, target_rating_before=rb_val,
            planned_target_rating=djay_val,
        )
    elif planned == "conflict":
        record["decision"] = "refused_conflict"
    else:
        record["decision"] = "skip"
    return record


def _write_provenance(
    out_path: Path,
    rows: list[dict],
    only_tracks: set[str] | None,
    *,
    prefer: str,
    written_rb: set[str],
    written_djay: set[str],
) -> None:
    """Write the machine-readable per-decision ratings apply result artifact."""
    decisions = []
    for row in rows:
        record = _row_provenance(row, only_tracks, prefer)
        if record["decision"] == "write":
            if record["target"] == "djay":
                record["written"] = record["djay_uuid"] in written_djay
            else:
                record["written"] = record["rb_content_id"] in written_rb
        decisions.append(record)
    payload = {
        "kind": "ratings-apply-result",
        "version": 1,
        "prefer": prefer,
        "written": {"rekordbox": len(written_rb), "djay": len(written_djay)},
        "decisions": decisions,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _rb_rating_verifier(db, expected: dict[str, int]):
    def _verify(track_id: str, _unused: object = None) -> bool:
        return _verify_rb_rating(db, track_id, expected[str(track_id)])

    return _verify


def _djay_rating_verifier(db_path: Path, expected: dict[str, int]):
    def _verify(track_id: str, _unused: object = None) -> bool:
        return _verify_djay_rating(db_path, track_id, expected[str(track_id)])

    return _verify


def _apply_rb_rating_targets(
    targets_rb: list[tuple[str, int]],
    *,
    rb_db_path: Path,
    flag_ok: bool,
    written_ids: set[str] | None = None,
) -> int:
    from apps.shared.rekordbox_db import open_db

    written_rb = 0
    db = open_db(rb_db_path)
    try:
        # Rail 4: post-write verify is routed through the session's
        # verifier callback. Mapping per-track track_id -> expected
        # Rating so the callback can readback on demand.
        rb_expected: dict[str, int] = {
            str(cid): int(rating) for cid, rating in targets_rb
        }
        with LiveWriteSession(
            target="rekordbox",
            reason="SYNC-06 bulk ratings (RB side)",
            flag_ok=flag_ok,
            db_path=rb_db_path,
            verifier=_rb_rating_verifier(db, rb_expected),
        ) as sess:
            # SYNC-12969: stage every row, then commit the batch ONCE. A
            # missing content row aborts before the commit so nothing lands.
            for content_id, rating in targets_rb:
                if not _write_rb_rating(db, content_id, rating):
                    rollback = getattr(db, "rollback", None)
                    if callable(rollback):
                        rollback()
                    raise SafetyAbort(
                        f"Rekordbox content row {content_id} not found; "
                        "batch aborted before commit."
                    )
            db.commit()
            for content_id, rating in targets_rb:
                with sess.per_track(content_id) as w:
                    w.write(rating)
                    if w.verify_readback():
                        w.append_reverse(
                            f"# revert RB rating for ContentID={content_id}"
                        )
                        written_rb += 1
                        if written_ids is not None:
                            written_ids.add(str(content_id))
    finally:
        try:
            db.close()
        except Exception:
            pass
    return written_rb


def _apply_djay_rating_targets(
    targets_djay: list[tuple[str, int]],
    *,
    djay_db_path: Path,
    flag_ok: bool,
    written_ids: set[str] | None = None,
) -> int:
    written_djay = 0
    djay_expected: dict[str, int] = {
        str(uuid): int(rating) for uuid, rating in targets_djay
    }
    with LiveWriteSession(
        target="djay",
        reason="SYNC-06 bulk ratings (djay side)",
        flag_ok=flag_ok,
        db_path=djay_db_path,
        verifier=_djay_rating_verifier(djay_db_path, djay_expected),
    ) as sess:
        for uuid, rating in targets_djay:
            with sess.per_track(uuid) as w:
                prior = _read_djay_rating(djay_db_path, uuid)
                ok = prior is not None and _write_djay_rating(
                    djay_db_path, uuid, rating
                )
                w.write(rating)
                if ok and prior is not None and w.verify_readback():
                    w.append_reverse(
                        f"# revert djay rating for uuid={uuid} "
                        f"({rating} -> {prior})\n"
                        + _djay_reverse_command(djay_db_path, uuid, prior)
                    )
                    w.append_reverse_record(
                        {
                            "target": "djay",
                            "field": "rating",
                            "track_id": uuid,
                            "prior": prior,
                            "new": int(rating),
                            "db_path": str(djay_db_path),
                        }
                    )
                    written_djay += 1
                    if written_ids is not None:
                        written_ids.add(str(uuid))
    return written_djay


def live_run(
    rows: list[dict],
    *,
    rb_db_path: Path,
    djay_db_path: Path,
    only_tracks: set[str] | None = None,
    flag_ok: bool = False,
    prefer: str = "newest",
    provenance_out: Path | None = None,
) -> int:
    # Note: rb_db_path and djay_db_path are REQUIRED (no defaults). The
    # previous signature defaulted to the WORKING-copy paths, which meant a
    # caller that forgot to route --live through _live_rb_db_path /
    # _live_djay_db_path would silently operate on the working copy even
    # when the user asked for --live. That was issue #1 (Phase 2 [I1]).
    # Keeping these required turns the footgun into an immediate TypeError.
    # ``prefer`` settles planned ``conflict`` rows (rb / djay); ``newest``
    # leaves them unwritten because the diff CSV has no timestamps.
    targets_rb, targets_djay = _plan_rating_targets(rows, only_tracks, prefer)
    written_rb = 0
    written_djay = 0
    written_rb_ids: set[str] = set()
    written_djay_ids: set[str] = set()
    if targets_rb:
        written_rb = _apply_rb_rating_targets(
            targets_rb, rb_db_path=rb_db_path, flag_ok=flag_ok,
            written_ids=written_rb_ids,
        )
    if targets_djay:
        written_djay = _apply_djay_rating_targets(
            targets_djay, djay_db_path=djay_db_path, flag_ok=flag_ok,
            written_ids=written_djay_ids,
        )
    if provenance_out is not None:
        _write_provenance(
            provenance_out, rows, only_tracks, prefer=prefer,
            written_rb=written_rb_ids, written_djay=written_djay_ids,
        )
    print(
        f"[apply_ratings] wrote rb={written_rb} djay={written_djay} "
        f"of {len(targets_rb) + len(targets_djay)} planned"
    )
    # SYNC-12971: a skipped or unverified planned write is a failure, so
    # shell automation cannot read partial application as success.
    if written_rb + written_djay < len(targets_rb) + len(targets_djay):
        print("[apply_ratings] some planned writes did not verify", file=sys.stderr)
        return 1
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
    parser.add_argument(
        "--prefer",
        choices=["rb", "djay", "newest"],
        default="newest",
        help=(
            "Side that wins a planned ratings conflict. 'newest' leaves "
            "conflicts unwritten (the diff CSV has no timestamps)."
        ),
    )
    parser.add_argument(
        "--provenance-out",
        type=Path,
        default=paths.DATA_DIR / "sync" / "ratings-apply-result.json",
        help="Per-decision rating write provenance artifact (live runs).",
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

    rows = load_diff_rows(args.diff_csv)
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
            provenance_out=args.provenance_out,
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
