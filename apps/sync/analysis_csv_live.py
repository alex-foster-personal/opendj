"""SYNC-05 CSV resolution live apply (rb-vs-djay), split from apply_analysis."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apps.sync.djay_writer import (
    patch_color_index,
    patch_key_signature_index,
    patch_manual_bpm,
    patch_tags,
)
from apps.sync.safety import LiveWriteSession


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


def _write_djay_field(db_path: Path, uuid: str, field: str, value) -> bool:
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
    if field == "key_camelot":
        return _camelot_to_djay_key_idx(str(value)) is not None and len(blob) > 0
    return len(blob) > 0


def live_run(
    rows: list[dict],
    *,
    fields: set[str] | None = None,
    only_tracks: set[str] | None = None,
    flag_ok: bool = False,
    rb_db_path: Path,
    djay_db_path: Path,
    open_rb_db: Callable[[Path], Any],
) -> int:
    from apps.sync.apply_analysis import (
        _SUPPORTED_RB_WRITE_FIELDS,
        UnsupportedRbFieldError,
        _verify_rb_field,
        _write_rb_field,
    )

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
        db = open_rb_db(rb_db_path)
        try:
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


__all__ = ["_camelot_to_djay_key_idx", "live_run"]
