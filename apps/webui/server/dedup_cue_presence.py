"""Bulk cue/hotcue/loop/beatgrid presence for dedup cluster members."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from apps.adapters.rekordbox import config
from apps.adapters.rekordbox.errors import _open_ro

_SQL_CHUNK = 500


@dataclass
class CuePresence:
    cue_count: int = 0
    hot_cue_count: int = 0
    loop_count: int = 0
    has_beatgrid: bool = False
    cue_positions_ms: list[int] = field(default_factory=list)


def _chunked(seq: Sequence[str], size: int = _SQL_CHUNK) -> Iterator[Sequence[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _empty_presence() -> CuePresence:
    return CuePresence()


def _presence_for(stable_id: str, result: dict[str, CuePresence]) -> CuePresence:
    return result.setdefault(stable_id, _empty_presence())


def _apply_cue_row(
    presence: CuePresence,
    *,
    kind: int | None,
    in_msec: int | None,
    out_msec: int | None,
) -> None:
    if kind is None or not 0 <= int(kind) <= 8:
        return
    presence.cue_count += 1
    if 1 <= int(kind) <= 8:
        presence.hot_cue_count += 1
    if out_msec is not None and int(out_msec) > 0:
        presence.loop_count += 1
    if in_msec is not None:
        presence.cue_positions_ms.append(int(in_msec))


def bulk_cue_presence(stable_ids: Sequence[str]) -> dict[str, CuePresence]:
    """Return per-stable_id cue presence from rekordbox djmdCue and beatgrid hints."""
    ids = list(dict.fromkeys(stable_ids))
    if not ids:
        return {}

    result: dict[str, CuePresence] = {}
    state_path = config.STATE_DB
    master_path = config.MASTER_PLAIN_DB
    if not state_path.exists():
        return result

    vendor_by_sid: dict[str, str] = {}
    try:
        state = _open_ro(state_path, "STATE_DB")
        try:
            for chunk in _chunked(ids):
                placeholders = ",".join("?" * len(chunk))
                for sid, vid in state.execute(
                    "SELECT stable_id, vendor_id FROM track_vendor_ids "
                    "WHERE vendor = 'rekordbox' AND deleted_at IS NULL "
                    f"AND stable_id IN ({placeholders})",
                    tuple(chunk),
                ):
                    vendor_by_sid[str(sid)] = str(vid)
        finally:
            state.close()
    except sqlite3.Error:
        return result

    if not vendor_by_sid:
        return result

    sid_by_vid: dict[str, list[str]] = {}
    for sid, vid in vendor_by_sid.items():
        sid_by_vid.setdefault(vid, []).append(sid)

    if master_path.exists():
        try:
            master = _open_ro(master_path, "MASTER_DB")
            try:
                vendor_ids = sorted(set(vendor_by_sid.values()))
                for chunk in _chunked(vendor_ids):
                    placeholders = ",".join("?" * len(chunk))
                    for content_id, kind, in_msec, out_msec in master.execute(
                        "SELECT ContentID, Kind, InMsec, OutMsec "
                        "FROM djmdCue "
                        "WHERE rb_local_deleted = 0 "
                        f"AND ContentID IN ({placeholders})",
                        tuple(chunk),
                    ):
                        for sid in sid_by_vid.get(str(content_id), []):
                            _apply_cue_row(
                                _presence_for(sid, result),
                                kind=kind,
                                in_msec=in_msec,
                                out_msec=out_msec,
                            )
                    for content_id, analysis_path in master.execute(
                        "SELECT ID, AnalysisDataPath FROM djmdContent "
                        "WHERE rb_local_deleted = 0 "
                        f"AND ID IN ({placeholders})",
                        tuple(chunk),
                    ):
                        if not analysis_path:
                            continue
                        for sid in sid_by_vid.get(str(content_id), []):
                            _presence_for(sid, result).has_beatgrid = True
            finally:
                master.close()
        except sqlite3.Error:
            pass

    try:
        state = _open_ro(state_path, "STATE_DB")
        try:
            table = state.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_canonical'"
            ).fetchone()
            if table is not None:
                for chunk in _chunked(ids):
                    placeholders = ",".join("?" * len(chunk))
                    for sid, in state.execute(
                        "SELECT DISTINCT stable_id FROM analysis_canonical "
                        "WHERE lane = 'beatgrid' "
                        f"AND stable_id IN ({placeholders})",
                        tuple(chunk),
                    ):
                        _presence_for(str(sid), result).has_beatgrid = True
        finally:
            state.close()
    except sqlite3.Error:
        pass

    for presence in result.values():
        presence.cue_positions_ms = sorted(presence.cue_positions_ms)[:16]

    return result
