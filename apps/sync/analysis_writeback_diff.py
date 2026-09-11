"""Read-only write-back dry-run planner for analysis projection fields."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.analysis.selection import (
    SelectionError,
    get_default,
    lane_is_promoted,
)
from apps.shared.harmonic import key_to_camelot
from apps.shared.state.db import open_ro

WRITEBACK_FIELDS: dict[str, str] = {
    "bpm": "beatgrid",
    "key": "key",
    "loudness_lufs": "loudness",
    "loudness_dbtp": "loudness",
}
WRITEBACK_LANES: tuple[str, ...] = ("beatgrid", "key", "loudness")

DENOMINATOR_LABEL = (
    "tracks resolvable in rekordbox that carry an own value for a promoted lane"
)

Bucket = Literal["writable", "no-own-value", "unmatched"]


class UnpromotedLaneError(RuntimeError):
    """Caller asked to dry-run a lane whose persisted default is not own."""


@dataclass(frozen=True, slots=True)
class WritebackRow:
    stable_id: str
    rb_content_id: str | None
    field: str
    lane: str
    bucket: Bucket
    rbx_value: str
    own_value: str
    delta: str
    promotion_state: str
    last_round_score: str


@dataclass(frozen=True, slots=True)
class WritebackPlan:
    denominator_n: int
    denominator_label: str
    rows: tuple[WritebackRow, ...]


def _open_rb_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _default_score_for(_lane: str) -> str:
    return "unposted"


def _validate_lanes(lanes: tuple[str, ...]) -> None:
    for lane in lanes:
        if lane not in WRITEBACK_LANES:
            raise SelectionError(
                f"lane {lane!r} is not a write-back lane; "
                f"lanes are {WRITEBACK_LANES}"
            )


def _refuse_unpromoted(state_conn: sqlite3.Connection, lanes: tuple[str, ...]) -> None:
    for lane in lanes:
        _validate_lanes((lane,))
        if not lane_is_promoted(state_conn, lane):
            raise UnpromotedLaneError(
                f"lane {lane!r} is not promoted "
                f"(persisted default={get_default(state_conn, lane)!r})"
            )


def _fields_for_lanes(
    fields: tuple[str, ...], lanes: tuple[str, ...]
) -> tuple[str, ...]:
    lane_set = set(lanes)
    return tuple(
        f for f in fields if f in WRITEBACK_FIELDS and WRITEBACK_FIELDS[f] in lane_set
    )


def _load_resolvable(
    state_conn: sqlite3.Connection,
    rb_conn: sqlite3.Connection,
    only_tracks: set[str] | None,
) -> dict[str, str]:
    """stable_id -> rekordbox content id for resolvable tracks."""
    content_ids = {
        row[0]
        for row in rb_conn.execute("SELECT ID FROM djmdContent").fetchall()
    }
    rows = state_conn.execute(
        """
        SELECT stable_id, vendor_id
        FROM track_vendor_ids
        WHERE vendor = 'rekordbox' AND deleted_at IS NULL
        """
    ).fetchall()
    out: dict[str, str] = {}
    for stable_id, vendor_id in rows:
        if vendor_id not in content_ids:
            continue
        if only_tracks is not None and stable_id not in only_tracks and vendor_id not in only_tracks:
            continue
        out[str(stable_id)] = str(vendor_id)
    return out


def _load_own_values(
    state_conn: sqlite3.Connection,
    fields: tuple[str, ...],
    only_tracks: set[str] | None,
) -> dict[tuple[str, str], object]:
    if not fields:
        return {}
    placeholders = ",".join("?" for _ in fields)
    rows = state_conn.execute(
        f"""
        SELECT ap.stable_id, ap.field, ap.value, tv.vendor_id
        FROM analysis_projection ap
        LEFT JOIN track_vendor_ids tv
          ON ap.stable_id = tv.stable_id
         AND tv.vendor = 'rekordbox'
         AND tv.deleted_at IS NULL
        WHERE ap.field IN ({placeholders})
          AND ap.status = 'ok'
          AND ap.value IS NOT NULL
        """,
        fields,
    ).fetchall()
    out: dict[tuple[str, str], object] = {}
    for stable_id, field, value, vendor_id in rows:
        sid = str(stable_id)
        if only_tracks is not None:
            vid = str(vendor_id) if vendor_id is not None else ""
            if sid not in only_tracks and vid not in only_tracks:
                continue
        out[(sid, str(field))] = value
    return out


def _rbx_bpm(rb_conn: sqlite3.Connection, content_id: str) -> float | None:
    row = rb_conn.execute(
        "SELECT BPM FROM djmdContent WHERE ID = ?", (content_id,)
    ).fetchone()
    if row is None or row[0] is None or int(row[0]) == 0:
        return None
    return int(row[0]) / 100.0


def _rbx_key_camelot(rb_conn: sqlite3.Connection, content_id: str) -> str | None:
    row = rb_conn.execute(
        """
        SELECT k.ScaleName
        FROM djmdContent c
        LEFT JOIN djmdKey k ON c.KeyID = k.ID
        WHERE c.ID = ?
        """,
        (content_id,),
    ).fetchone()
    if row is None or not row[0]:
        return None
    scale = str(row[0])
    try:
        return str(key_to_camelot(scale))
    except (ValueError, TypeError):
        return scale


def _format_bpm(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def _bpm_delta(rbx: float | None, own: object) -> str:
    if rbx is None:
        return "-"
    try:
        own_f = float(own)
    except (TypeError, ValueError):
        return "?"
    diff = own_f - rbx
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:.2f}"


def _key_delta(rbx_camelot: str | None, rbx_raw: str | None, own: object) -> str:
    own_s = str(own)
    if rbx_camelot is None:
        return "?"
    if rbx_camelot == own_s:
        return "same"
    rbx_display = rbx_camelot if rbx_raw is None else rbx_camelot
    return f"{rbx_display} -> {own_s}"


def _field_values(
    rb_conn: sqlite3.Connection,
    content_id: str | None,
    field: str,
    own: object | None,
) -> tuple[str, str, str]:
    if field == "bpm":
        rbx_f = _rbx_bpm(rb_conn, content_id) if content_id else None
        rbx_s = _format_bpm(rbx_f)
        own_s = str(own) if own is not None else "-"
        delta = _bpm_delta(rbx_f, own) if own is not None else "-"
        return rbx_s, own_s, delta
    if field == "key":
        rbx_raw: str | None = None
        rbx_camelot: str | None = None
        if content_id:
            row = rb_conn.execute(
                """
                SELECT k.ScaleName
                FROM djmdContent c
                LEFT JOIN djmdKey k ON c.KeyID = k.ID
                WHERE c.ID = ?
                """,
                (content_id,),
            ).fetchone()
            if row and row[0]:
                rbx_raw = str(row[0])
                try:
                    rbx_camelot = str(key_to_camelot(rbx_raw))
                except (ValueError, TypeError):
                    rbx_camelot = None
        rbx_s = rbx_camelot if rbx_camelot is not None else (rbx_raw or "-")
        own_s = str(own) if own is not None else "-"
        if own is None:
            delta = "-"
        elif rbx_camelot is None and rbx_raw:
            delta = "?"
        else:
            delta = _key_delta(rbx_camelot, rbx_raw, own)
        return rbx_s, own_s, delta
    if field in ("loudness_lufs", "loudness_dbtp"):
        own_s = str(own) if own is not None else "-"
        return "-", own_s, "n/a" if own is not None else "-"
    return "-", str(own) if own is not None else "-", "-"


def plan_writeback(
    state_conn: sqlite3.Connection,
    rb_conn: sqlite3.Connection,
    *,
    lanes: tuple[str, ...],
    fields: tuple[str, ...],
    only_tracks: set[str] | None = None,
    score_for: Callable[[str], str] | None = None,
) -> WritebackPlan:
    _refuse_unpromoted(state_conn, lanes)
    active_fields = _fields_for_lanes(fields, lanes)
    score_fn = score_for or _default_score_for

    resolvable = _load_resolvable(state_conn, rb_conn, only_tracks)
    own_values = _load_own_values(state_conn, active_fields, only_tracks)

    stable_ids = set(resolvable) | {sid for sid, _ in own_values}
    rows: list[WritebackRow] = []

    for stable_id in sorted(stable_ids):
        content_id = resolvable.get(stable_id)
        is_resolvable = content_id is not None
        for field in active_fields:
            lane = WRITEBACK_FIELDS[field]
            own = own_values.get((stable_id, field))
            has_own = own is not None
            promotion = get_default(state_conn, lane)
            last_score = score_fn(lane)

            if has_own and not is_resolvable:
                bucket: Bucket = "unmatched"
            elif is_resolvable and not has_own:
                bucket = "no-own-value"
            elif has_own and is_resolvable:
                bucket = "writable"
            else:
                continue

            rbx_s, own_s, delta = _field_values(rb_conn, content_id, field, own)
            rows.append(
                WritebackRow(
                    stable_id=stable_id,
                    rb_content_id=content_id,
                    field=field,
                    lane=lane,
                    bucket=bucket,
                    rbx_value=rbx_s,
                    own_value=own_s,
                    delta=delta,
                    promotion_state=promotion,
                    last_round_score=last_score,
                )
            )

    denominator_ids: set[str] = set()
    for stable_id in resolvable:
        for field in active_fields:
            if (stable_id, field) in own_values:
                denominator_ids.add(stable_id)
                break

    return WritebackPlan(
        denominator_n=len(denominator_ids),
        denominator_label=DENOMINATOR_LABEL,
        rows=tuple(rows),
    )


def render_plan(plan: WritebackPlan) -> None:
    counts = {"writable": 0, "no-own-value": 0, "unmatched": 0}
    for row in plan.rows:
        counts[row.bucket] += 1

    print("[apply_analysis] write-back dry-run")
    print(f"denominator: {plan.denominator_n} {plan.denominator_label}")
    print(f"writable: {counts['writable']}")
    print(f"no-own-value: {counts['no-own-value']}")
    print(f"unmatched: {counts['unmatched']}")
    print()
    print(
        "stable_id  field          rbx     own     delta   lane      "
        "promoted  last_round  bucket"
    )
    for row in plan.rows:
        print(
            f"{row.stable_id:<10} {row.field:<14} {row.rbx_value:<7} "
            f"{row.own_value:<7} {row.delta:<7} {row.lane:<9} "
            f"{row.promotion_state:<9} {row.last_round_score:<11} {row.bucket}"
        )


def dry_run(
    *,
    state_db: Path,
    rb_db: Path,
    lanes: tuple[str, ...] = WRITEBACK_LANES,
    fields: tuple[str, ...] = tuple(WRITEBACK_FIELDS),
    only_tracks: set[str] | None = None,
    score_for: Callable[[str], str] | None = None,
) -> int:
    if not state_db.exists():
        raise FileNotFoundError(f"state database not found: {state_db}")
    if not rb_db.exists():
        raise FileNotFoundError(f"rekordbox database not found: {rb_db}")

    state_conn = open_ro(state_db)
    rb_conn = _open_rb_ro(rb_db)
    try:
        plan = plan_writeback(
            state_conn,
            rb_conn,
            lanes=lanes,
            fields=fields,
            only_tracks=only_tracks,
            score_for=score_for,
        )
    finally:
        state_conn.close()
        rb_conn.close()

    render_plan(plan)
    return 0


__all__ = [
    "UnpromotedLaneError",
    "WritebackPlan",
    "WritebackRow",
    "WRITEBACK_FIELDS",
    "WRITEBACK_LANES",
    "dry_run",
    "plan_writeback",
    "render_plan",
]
