"""Phase 4 SYNC-05 analysis-field bulk sync CLI."""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from apps.analysis.selection import SelectionError
from apps.shared import paths
from apps.shared.rekordbox_writeback import require_writeback_enabled
from apps.sync.analysis_writeback_diff import (
    UnpromotedLaneError,
    WRITEBACK_FIELDS,
    WRITEBACK_LANES,
    dry_run as writeback_dry_run,
)
from apps.sync.djay_writer import (
    patch_color_index,
    patch_key_signature_index,
    patch_manual_bpm,
    patch_tags,
)
from apps.sync.safety import LiveWriteSession, SafetyAbort

# Codex P04-02: RB-side writes are only implemented for these fields.
# ``apply_analysis`` used to silently skip any other field and still
# exit 0, which made the CLI report success after a partial apply.
# See ``_write_rb_field`` and ``live_run`` below: unsupported RB fields
# now fail the run with a non-zero exit instead of being dropped.
_SUPPORTED_RB_WRITE_FIELDS = frozenset({"bpm", "energy"})


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
    resolved_fields = fields or tuple(WRITEBACK_FIELDS)
    return writeback_dry_run(
        state_db=state_db or paths.STATE_DB,
        rb_db=rb_db or paths.REKORDBOX_PLAIN_DB,
        lanes=lanes,
        fields=resolved_fields,
        only_tracks=only_tracks,
    )


def _camelot_to_djay_key_idx(camelot: str) -> int | None:
    from apps.shared.djay_db import _DJAY_KEY_IDX_TO_STD
    from apps.shared.harmonic import key_to_camelot

    for idx, std in _DJAY_KEY_IDX_TO_STD.items():
        try:
            if str(key_to_camelot(std)) == camelot:
                return idx
        except ValueError:
            continue
    return None


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
    try:
        content = db.get_content(ID=str(content_id)).one()
    except Exception:
        return False
    if field == "bpm":
        try:
            content.BPM = int(round(float(value) * 100))
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
    try:
        content = db.get_content(ID=str(content_id)).one()
    except Exception:
        return False
    try:
        if field == "bpm":
            return int(getattr(content, "BPM", -1)) == int(
                round(float(value) * 100)
            )
        if field == "energy":
            return int(getattr(content, "ColorID", -1)) == int(value)
    except (TypeError, ValueError):
        return False
    return False


def _write_djay_field(db_path: Path, uuid: str, field: str, value) -> bool:
    import sqlite3

    collection = (
        "mediaItemAnalyzedData"
        if field in ("manual_bpm", "key_camelot", "bpm")
        else "mediaItemUserData"
    )
    with sqlite3.connect(str(db_path)) as con:
        row = con.execute(
            "SELECT data FROM database2 WHERE collection = ? AND key = ?",
            (collection, uuid),
        ).fetchone()
        if not row:
            return False
        blob = row[0] or b""
        if field == "manual_bpm":
            try:
                new_blob = patch_manual_bpm(blob, float(value))
            except (TypeError, ValueError):
                return False
        elif field == "key_camelot":
            idx = _camelot_to_djay_key_idx(str(value))
            if idx is None:
                return False
            new_blob = patch_key_signature_index(blob, idx)
        elif field == "energy":
            try:
                new_blob = patch_color_index(blob, int(value))
            except (TypeError, ValueError):
                return False
        elif field == "tags":
            new_blob = patch_tags(blob, str(value))
        else:
            return False
        con.execute(
            "UPDATE database2 SET data = ? WHERE collection = ? AND key = ?",
            (new_blob, collection, uuid),
        )
        con.commit()
    return True


def _verify_djay_field(db_path: Path, uuid: str, field: str, value) -> bool:
    """Rail 4 (post-write verify) for the djay side.

    Re-reads the TSAF blob and checks that:

    * the row still exists in the expected collection, and
    * the stored blob differs structurally from an empty/absent payload
      (the per-field patch helpers in :mod:`apps.sync.djay_writer`
      mutate known byte offsets, so a non-empty blob after write is the
      strongest invariant we can cheaply assert without re-implementing
      the TSAF parser here).

    The more expensive per-field byte-level verification happens inside
    :func:`_write_djay_field` itself via the patch helpers; this verify
    step catches the "row vanished after write" and "blob was emptied
    after write" failure modes.
    """
    import sqlite3

    collection = (
        "mediaItemAnalyzedData"
        if field in ("manual_bpm", "key_camelot", "bpm")
        else "mediaItemUserData"
    )
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            row = con.execute(
                "SELECT data FROM database2 "
                "WHERE collection = ? AND key = ?",
                (collection, uuid),
            ).fetchone()
    except Exception:
        return False
    if not row:
        return False
    blob = row[0] or b""
    # Key-by-key structural checks.
    if field == "key_camelot":
        return _camelot_to_djay_key_idx(str(value)) is not None \
            and len(blob) > 0
    # All other fields: we require a non-empty blob (writes that left
    # the row truncated would fail here) and rely on the writer's own
    # return value for byte-exact validation.
    return len(blob) > 0


def live_run(
    rows: list[dict],
    *,
    fields: set[str] | None = None,
    only_tracks: set[str] | None = None,
    flag_ok: bool = False,
    rb_db_path: Path,
    djay_db_path: Path,
) -> int:
    # v1.0 adversarial review (#1, CRITICAL): ``rb_db_path`` and
    # ``djay_db_path`` are required -- callers MUST decide live vs
    # working explicitly via ``_live_rb_db_path`` / ``_live_djay_db_path``.
    # Default values were removed here so a missing kwarg in ``main`` is
    # a loud TypeError instead of a silent WORKING-DB misroute.
    from apps.shared.rekordbox_db import open_db

    written = 0
    rb_plan: list[tuple[str, str, object]] = []
    djay_plan: list[tuple[str, str, object]] = []
    for r in rows:
        field = r.get("field", "")
        if fields is not None and field not in fields:
            continue
        rb_id = r.get("rb_content_id", "")
        djay_uuid = r.get("djay_uuid", "")
        if only_tracks is not None and rb_id not in only_tracks and djay_uuid not in only_tracks:
            continue
        res = r.get("resolution", "")
        if res == "accept_rb" and djay_uuid:
            djay_plan.append((djay_uuid, field, r.get("rb_value", "")))
        elif res == "accept_djay" and rb_id:
            rb_plan.append((rb_id, field, r.get("djay_value", "")))

    # Codex P04-02: refuse to open a live session if the plan contains
    # any RB-side field we can't actually write. Previously these rows
    # were silently skipped inside ``_write_rb_field`` and the CLI
    # exited 0, misreporting a partial apply as success.
    unsupported_rb = sorted({
        fld for _cid, fld, _val in rb_plan
        if fld not in _SUPPORTED_RB_WRITE_FIELDS
    })
    if unsupported_rb:
        raise UnsupportedRbFieldError(
            "Refusing to apply: RB-side writes not implemented for "
            f"fields {unsupported_rb}. Re-run with --fields limited to "
            f"{sorted(_SUPPORTED_RB_WRITE_FIELDS)} or resolve those "
            "rows on the djay side first."
        )

    if rb_plan:
        db = open_db(rb_db_path)
        try:
            # Rail 4: per-track verifier keyed by the "content_id:field"
            # tag so the session can read back the RB column after each
            # write. Populated before opening the session so the closure
            # has a stable mapping.
            rb_expected: dict[str, tuple[str, object]] = {
                f"{cid}:{fld}": (fld, val) for cid, fld, val in rb_plan
            }

            def rb_verifier(tag: str, _unused: object = None) -> bool:
                fld, val = rb_expected[tag]
                cid = tag.split(":", 1)[0]
                return _verify_rb_field(db, cid, fld, val)

            with LiveWriteSession(
                target="rekordbox",
                reason="SYNC-05 analysis sync (RB side)",
                flag_ok=flag_ok,
                db_path=rb_db_path,
                verifier=rb_verifier,
            ) as sess:
                for content_id, field, value in rb_plan:
                    with sess.per_track(f"{content_id}:{field}") as w:
                        ok = _write_rb_field(db, content_id, field, value)
                        w.write((field, value))
                        if ok and w.verify_readback():
                            w.append_reverse(
                                f"# revert RB {field} for ContentID={content_id}"
                            )
                            written += 1
        finally:
            try:
                db.close()
            except Exception:
                pass

    if djay_plan:
        djay_expected: dict[str, tuple[str, object]] = {
            f"{uuid}:{fld}": (fld, val) for uuid, fld, val in djay_plan
        }

        def djay_verifier(tag: str, _unused: object = None) -> bool:
            fld, val = djay_expected[tag]
            uuid = tag.split(":", 1)[0]
            return _verify_djay_field(djay_db_path, uuid, fld, val)

        with LiveWriteSession(
            target="djay",
            reason="SYNC-05 analysis sync (djay side)",
            flag_ok=flag_ok,
            db_path=djay_db_path,
            verifier=djay_verifier,
        ) as sess:
            for uuid, field, value in djay_plan:
                with sess.per_track(f"{uuid}:{field}") as w:
                    ok = _write_djay_field(djay_db_path, uuid, field, value)
                    w.write((field, value))
                    if ok and w.verify_readback():
                        w.append_reverse(f"# revert djay {field} for uuid={uuid}")
                        written += 1

    print(
        f"[apply_analysis] wrote {written} fields of "
        f"{len(rb_plan) + len(djay_plan)} planned"
    )
    return 0


_LIVE_DEFAULT_FIELDS = "bpm,manual_bpm,key_camelot,energy,tags"
_WRITEBACK_DEFAULT_FIELDS = "bpm,key,loudness_lufs,loudness_dbtp"


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
        help="Write-back dry-run lanes (ignored with --live).",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=paths.STATE_DB,
        help="State DB for write-back dry-run (ignored with --live).",
    )
    parser.add_argument(
        "--rb-db",
        type=Path,
        default=paths.REKORDBOX_PLAIN_DB,
        help="Rekordbox plain working copy for write-back dry-run (ignored with --live).",
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

    fields_raw = args.fields.strip()
    if fields_raw:
        fields_list = tuple(f.strip() for f in fields_raw.split(",") if f.strip())
    elif args.live:
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

    if not args.live:
        if args.diff_csv is not None:
            return _csv_resolution_summary(_load_diff(args.diff_csv))
        lanes = tuple(l.strip() for l in args.lanes.split(",") if l.strip())
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

    csv_path = args.diff_csv or (paths.DATA_DIR / "sync" / "analysis-diff.csv")
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

    try:
        rc = live_run(
            rows,
            fields=fields,
            only_tracks=only_tracks,
            flag_ok=args.i_understand_the_risks,
            rb_db_path=_live_rb_db_path(args.live),
            djay_db_path=_live_djay_db_path(args.live),
        )
    except SafetyAbort as e:
        print(f"[apply_analysis] SafetyAbort: {e}", file=sys.stderr)
        return 3
    except UnsupportedRbFieldError as e:
        # Codex P04-02: non-zero exit on unsupported RB-side fields so
        # the CLI no longer reports success after a partial apply.
        print(f"[apply_analysis] UnsupportedRbField: {e}", file=sys.stderr)
        return 4

    if rc == 0 and only_tracks and not args.bulk:
        mark_cautious_success("apply_analysis")
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["UnpromotedLaneError", "dry_run", "live_run", "main"]
