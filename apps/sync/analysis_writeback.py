"""Live write-back of promoted analysis scalars into rekordbox.

Key writes retarget ``djmdContent.KeyID`` onto an existing or new ``djmdKey``
row. Loudness has no rekordbox column; values are stored in ``odjAnalysisScalar``
inside the same sqlite file (rekordbox ignores unknown tables; CDJs do not
display LUFS). USB read-back of the sidecar is out of scope (#2051).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.adapters.rekordbox.writer import restore_content_field, snapshot_content_field
from apps.analysis_key import canon
from apps.shared.harmonic import key_to_camelot
from apps.shared.state.db import open_ro
from apps.sync.analysis_writeback_diff import (
    WritebackPlan,
    _open_rb_ro,
    plan_writeback,
    render_plan,
)
from apps.sync.safety import LiveWriteSession, SafetyAbort, require_writeback_plan

_SCALAR_TABLE = "odjAnalysisScalar"
_FLOAT_TOL = 1e-6


def writable_plan_hash(plan: WritebackPlan) -> str:
    writable = [
        {
            "stable_id": row.stable_id,
            "field": row.field,
            "rb_content_id": row.rb_content_id,
            "own_value": row.own_value,
        }
        for row in plan.rows
        if row.bucket == "writable"
    ]
    writable.sort(key=lambda item: (item["stable_id"], item["field"]))
    payload = json.dumps(writable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _ensure_scalar_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_SCALAR_TABLE} (
            ContentID TEXT NOT NULL,
            field TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (ContentID, field)
        )
        """
    )


def _new_key_id(conn: sqlite3.Connection) -> str:
    for _ in range(50):
        candidate = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
        if conn.execute(
            "SELECT 1 FROM djmdKey WHERE ID = ?", (candidate,)
        ).fetchone() is None:
            return candidate
    raise RuntimeError("could not allocate a unique djmdKey ID")


def _resolve_key_id(conn: sqlite3.Connection, own_camelot: str) -> tuple[str, bool]:
    """Return (key_id, created). Never mutates an existing catalog row."""
    try:
        spelled = canon.to_rekordbox_scale_name(canon.from_camelot(str(own_camelot)))
    except (ValueError, TypeError):
        raise ValueError(f"unparseable own key: {own_camelot!r}") from None

    row = conn.execute(
        "SELECT ID FROM djmdKey WHERE ScaleName = ?", (spelled,)
    ).fetchone()
    if row:
        return str(row[0]), False

    for key_id, scale_name in conn.execute(
        "SELECT ID, ScaleName FROM djmdKey"
    ).fetchall():
        try:
            if str(key_to_camelot(str(scale_name))) == str(own_camelot):
                return str(key_id), False
        except (ValueError, TypeError):
            continue

    new_id = _new_key_id(conn)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO djmdKey (ID, ScaleName) VALUES (?, ?)",
        (new_id, spelled),
    )
    return new_id, True


def write_scalar(
    conn: sqlite3.Connection,
    content_id: str,
    field: str,
    value: object,
    snapshot: dict[str, Any] | None = None,
) -> bool:
    if field == "bpm":
        try:
            bpm_raw = round(float(value) * 100)
        except (TypeError, ValueError):
            return False
        result = conn.execute(
            "UPDATE djmdContent SET BPM = ? WHERE ID = ?",
            (bpm_raw, content_id),
        )
        return result.rowcount == 1

    if field == "key":
        try:
            key_id, created = _resolve_key_id(conn, str(value))
        except ValueError:
            return False
        result = conn.execute(
            "UPDATE djmdContent SET KeyID = ? WHERE ID = ?",
            (key_id, content_id),
        )
        if result.rowcount != 1:
            return False
        if created and snapshot is not None:
            snapshot["created_key_id"] = key_id
        return True

    if field in ("loudness_lufs", "loudness_dbtp"):
        _ensure_scalar_table(conn)
        try:
            stored = str(float(value))
        except (TypeError, ValueError):
            return False
        now = datetime.now(UTC).isoformat()
        conn.execute(
            f"""
            INSERT INTO {_SCALAR_TABLE} (ContentID, field, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ContentID, field) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (content_id, field, stored, now),
        )
        return True

    return False


def verify_scalar(
    conn: sqlite3.Connection,
    content_id: str,
    field: str,
    value: object,
) -> bool:
    if field == "bpm":
        row = conn.execute(
            "SELECT BPM FROM djmdContent WHERE ID = ?", (content_id,)
        ).fetchone()
        if row is None:
            return False
        try:
            return int(row[0]) == round(float(value) * 100)
        except (TypeError, ValueError):
            return False

    if field == "key":
        row = conn.execute(
            """
            SELECT k.ScaleName
            FROM djmdContent c
            LEFT JOIN djmdKey k ON c.KeyID = k.ID
            WHERE c.ID = ?
            """,
            (content_id,),
        ).fetchone()
        if row is None or not row[0]:
            return False
        try:
            return str(key_to_camelot(str(row[0]))) == str(value)
        except (ValueError, TypeError):
            return False

    if field in ("loudness_lufs", "loudness_dbtp"):
        row = conn.execute(
            f"SELECT value FROM {_SCALAR_TABLE} WHERE ContentID = ? AND field = ?",
            (content_id, field),
        ).fetchone()
        if row is None:
            return False
        try:
            return abs(float(row[0]) - float(value)) <= _FLOAT_TOL
        except (TypeError, ValueError):
            return False

    return False


def _maybe_delete_created_key(conn: sqlite3.Connection, key_id: str) -> None:
    row = conn.execute(
        "SELECT COUNT(*) FROM djmdContent WHERE KeyID = ?", (key_id,)
    ).fetchone()
    if row and int(row[0]) == 0:
        conn.execute("DELETE FROM djmdKey WHERE ID = ?", (key_id,))


def undo_writeback(conn: sqlite3.Connection, snapshots: Sequence[Mapping[str, Any]]) -> None:
    from apps.sync.analysis_writeback_pqtz import restore_pqtz_dat

    for snap in reversed(list(snapshots)):
        if str(snap.get("field")) == "pqtz":
            restore_pqtz_dat(snap)
            continue
        restore_content_field(conn, snap)
        created = snap.get("created_key_id")
        if created:
            _maybe_delete_created_key(conn, str(created))


def _parse_own_value(field: str, own_value: str) -> object:
    if field == "bpm":
        return float(own_value)
    if field in ("loudness_lufs", "loudness_dbtp"):
        return float(own_value)
    return own_value


def build_writeback_plan(
    *,
    state_db: Path,
    rb_db_path: Path,
    lanes: tuple[str, ...],
    fields: tuple[str, ...],
    only_tracks: set[str] | None,
) -> WritebackPlan:
    if not state_db.exists():
        raise FileNotFoundError(f"state database not found: {state_db}")
    if not rb_db_path.exists():
        raise FileNotFoundError(f"rekordbox database not found: {rb_db_path}")

    state_conn = open_ro(state_db)
    rb_ro = _open_rb_ro(rb_db_path)
    try:
        return plan_writeback(
            state_conn,
            rb_ro,
            lanes=lanes,
            fields=fields,
            only_tracks=only_tracks,
        )
    finally:
        state_conn.close()
        rb_ro.close()


def validate_writeback_plan(plan: WritebackPlan) -> None:
    render_plan(plan)
    require_writeback_plan("apply_analysis", writable_plan_hash(plan))


def live_writeback(
    *,
    plan: WritebackPlan,
    state_db: Path,
    rb_db_path: Path,
    rb_conn: sqlite3.Connection,
    fields: tuple[str, ...],
    flag_ok: bool,
) -> int:
    from apps.sync.apply_analysis import (
        _SUPPORTED_RB_WRITE_FIELDS,
        UnsupportedRbFieldError,
    )

    writable_rows = [
        row
        for row in plan.rows
        if row.bucket == "writable"
        and row.field in fields
        and row.field in _SUPPORTED_RB_WRITE_FIELDS
        and row.rb_content_id is not None
    ]

    unsupported = sorted({
        row.field
        for row in plan.rows
        if row.bucket == "writable" and row.field not in _SUPPORTED_RB_WRITE_FIELDS
    })
    if unsupported:
        raise UnsupportedRbFieldError(
            f"RB-side write for fields {unsupported} is not implemented; "
            f"supported: {sorted(_SUPPORTED_RB_WRITE_FIELDS)}."
        )

    dynamic_pqtz = sum(
        1
        for row in plan.rows
        if row.bucket == "dynamic" and row.field == "pqtz" and row.field in fields
    )
    if dynamic_pqtz:
        print(
            f"[apply_analysis] refused {dynamic_pqtz} dynamic PQTZ grid(s) "
            "(multi-anchor; see #1481)"
        )

    if not writable_rows:
        print("[apply_analysis] wrote 0 fields")
        return 0

    from apps.shared.state.db import open_ro
    from apps.sync.analysis_writeback_pqtz import load_own_beatgrid, write_pqtz_row

    state_conn = open_ro(state_db)
    preimages: list[dict[str, Any]] = []
    written = 0
    expected: dict[str, tuple[str, str, object]] = {}
    pqtz_beats: dict[str, list[dict[str, object]]] = {}

    for row in writable_rows:
        content_id = str(row.rb_content_id)
        if row.field == "pqtz":
            payload = load_own_beatgrid(state_conn, row.stable_id)
            if payload is None:
                raise SafetyAbort(
                    f"pqtz own beatgrid missing for {row.stable_id}"
                )
            beats = list(payload["beats"])
            pqtz_beats[content_id] = beats
            tag = f"{content_id}:{row.field}"
            expected[tag] = (content_id, row.field, beats)
            continue
        own = _parse_own_value(row.field, row.own_value)
        tag = f"{content_id}:{row.field}"
        expected[tag] = (content_id, row.field, own)

    def verifier(tag: str, _unused: object = None) -> bool:
        content_id, fld, val = expected[tag]
        if fld == "pqtz":
            from apps.sync.analysis_writeback_pqtz import (
                resolve_analysis_dat_path,
            )
            from apps.sync.analysis_writeback_pqtz import (
                verify_pqtz as _verify_pqtz,
            )

            dat_path = resolve_analysis_dat_path(rb_conn, content_id)
            if dat_path is None:
                return False
            return _verify_pqtz(dat_path, val)
        return verify_scalar(rb_conn, content_id, fld, val)

    with LiveWriteSession(
        target="rekordbox",
        reason="write-back key/loudness/pqtz",
        flag_ok=flag_ok,
        db_path=rb_db_path,
        verifier=verifier,
    ) as sess:
        _ensure_scalar_table(rb_conn)
        preimage_path = sess.reverse_script_path.parent / "analysis_preimages.json"
        try:
            for row in writable_rows:
                content_id = str(row.rb_content_id)
                field = row.field
                tag = f"{content_id}:{field}"
                if field == "pqtz":
                    beats = pqtz_beats[content_id]
                    preimage: dict[str, Any] = {
                        "field": "pqtz",
                        "content_id": content_id,
                    }
                    with sess.per_track(tag) as w:
                        write_pqtz_row(
                            rb_conn, content_id, beats, preimage=preimage,
                        )
                        w.write((field, beats))
                        if not w.verify_readback():
                            raise SafetyAbort(
                                f"verify_readback failed for {tag}"
                            )
                        preimages.append(dict(preimage))
                        w.append_reverse(
                            f"# analysis preimage recorded in {preimage_path.name}"
                        )
                        written += 1
                    continue
                own = _parse_own_value(field, row.own_value)
                preimage = snapshot_content_field(rb_conn, content_id, field)
                with sess.per_track(tag) as w:
                    ok = write_scalar(rb_conn, content_id, field, own, preimage)
                    if not ok:
                        raise SafetyAbort(
                            f"write_scalar failed for {tag} field={field!r}"
                        )
                    w.write((field, own))
                    if not verify_scalar(rb_conn, content_id, field, own):
                        restore_content_field(rb_conn, preimage)
                        created = preimage.get("created_key_id")
                        if created:
                            _maybe_delete_created_key(rb_conn, str(created))
                    if not w.verify_readback():
                        raise SafetyAbort(f"verify_readback failed for {tag}")
                    preimages.append(dict(preimage))
                    w.append_reverse(
                        f"# analysis preimage recorded in {preimage_path.name}"
                    )
                    written += 1
        finally:
            state_conn.close()
        preimage_path.write_text(
            json.dumps(preimages, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rb_conn.commit()

    print(
        f"[apply_analysis] wrote {written} fields of "
        f"{len(writable_rows)} planned"
    )
    return 0


def run_undo(
    preimage_path: Path,
    *,
    rb_db_path: Path,  # noqa: ARG001 - retained for the caller's keyword contract
    rb_conn: sqlite3.Connection,
) -> int:
    from apps.sync.analysis_writeback_pqtz import restore_pqtz_dat, snapshot_pqtz_dat
    from apps.sync.safety import assert_target_not_running

    assert_target_not_running("rekordbox")
    snapshots = json.loads(preimage_path.read_text(encoding="utf-8"))
    undo_writeback(rb_conn, snapshots)
    for snap in snapshots:
        field = str(snap["field"])
        if field == "pqtz":
            dat_path = Path(str(snap["dat_path"]))
            restore_pqtz_dat(snap)
            expected = snapshot_pqtz_dat(dat_path)
            if expected.get("dat_bytes_b64") != snap.get("dat_bytes_b64"):
                raise SafetyAbort(f"undo verify failed: PQTZ for {dat_path}")
            continue
        content_id = str(snap["content_id"])
        if field == "key":
            row = rb_conn.execute(
                "SELECT KeyID FROM djmdContent WHERE ID = ?", (content_id,)
            ).fetchone()
            if row is None or str(row[0]) != str(snap.get("key_id")):
                raise SafetyAbort(
                    f"undo verify failed: KeyID for {content_id}"
                )
        elif field == "bpm":
            row = rb_conn.execute(
                "SELECT BPM FROM djmdContent WHERE ID = ?", (content_id,)
            ).fetchone()
            if row is None or int(row[0]) != int(snap.get("bpm")):
                raise SafetyAbort(f"undo verify failed: BPM for {content_id}")
        elif field in ("loudness_lufs", "loudness_dbtp"):
            if snap.get("scalar_existed"):
                row = rb_conn.execute(
                    f"SELECT value FROM {_SCALAR_TABLE} "
                    "WHERE ContentID = ? AND field = ?",
                    (content_id, field),
                ).fetchone()
                if row is None or str(row[0]) != str(snap.get("scalar_value")):
                    raise SafetyAbort(
                        f"undo verify failed: sidecar {field} for {content_id}"
                    )
            else:
                row = rb_conn.execute(
                    f"SELECT 1 FROM {_SCALAR_TABLE} "
                    "WHERE ContentID = ? AND field = ?",
                    (content_id, field),
                ).fetchone()
                if row is not None:
                    raise SafetyAbort(
                        f"undo verify failed: sidecar {field} still present"
                    )
    rb_conn.commit()
    print(f"[apply_analysis] undo restored {len(snapshots)} field(s)")
    return 0


def dry_run_writeback(
    *,
    state_db: Path,
    rb_db: Path,
    lanes: tuple[str, ...],
    fields: tuple[str, ...],
    only_tracks: set[str] | None,
) -> int:
    from apps.sync.safety import mark_writeback_plan

    plan = build_writeback_plan(
        state_db=state_db,
        rb_db_path=rb_db,
        lanes=lanes,
        fields=fields,
        only_tracks=only_tracks,
    )
    render_plan(plan)
    mark_writeback_plan("apply_analysis", writable_plan_hash(plan))
    return 0


__all__ = [
    "build_writeback_plan",
    "dry_run_writeback",
    "live_writeback",
    "run_undo",
    "undo_writeback",
    "validate_writeback_plan",
    "verify_scalar",
    "writable_plan_hash",
    "write_scalar",
]
