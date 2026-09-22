"""Fixed-tempo PQTZ beatgrid write-back for promoted own grids (#2050)."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from construct import Container
from pyrekordbox.anlz import AnlzFile, structs
from pyrekordbox.anlz.tags import PQTZAnlzTag

from apps.analysis.canonical import canonical_pointer
from apps.analysis.record import AnalysisRecord
from apps.sync.safety import SafetyAbort

PQTZ_TEMPO_SCALE = 100
PQTZ_TIME_SCALE = 1000
SERVED_BPM_DECIMALS = 2
SERVED_T_DECIMALS = 3
_ANLZ_LEN_HEADER = 28
_PQTZ_LEN_HEADER = 24
_PQTZ_U2 = 0x80000


def served_beats_from_pqtz_tag(pqtz: PQTZAnlzTag) -> list[dict[str, float | int]]:
    beats = pqtz.get_beats()
    bpms = pqtz.get_bpms()
    times = [float(t) for t in pqtz.get_times()]
    return [
        {
            "n": int(n),
            "bpm": round(float(bpm), SERVED_BPM_DECIMALS),
            "t": round(t, SERVED_T_DECIMALS),
        }
        for n, bpm, t in zip(beats, bpms, times, strict=True)
    ]


def quantize_own_beat(beat: Mapping[str, object]) -> tuple[int, int, int]:
    n = int(beat["n"])
    tempo = round(float(beat["bpm"]) * PQTZ_TEMPO_SCALE)
    time_ms = round(float(beat["t"]) * PQTZ_TIME_SCALE)
    if n not in (1, 2, 3, 4):
        raise ValueError(f"beat n must be 1..4, got {n}")
    return n, tempo, time_ms


def served_beats_from_own(own_beats: Sequence[Mapping[str, object]]) -> list[dict[str, float | int]]:
    return [
        {
            "n": n,
            "bpm": round(tempo / PQTZ_TEMPO_SCALE, SERVED_BPM_DECIMALS),
            "t": round(time_ms / PQTZ_TIME_SCALE, SERVED_T_DECIMALS),
        }
        for n, tempo, time_ms in (quantize_own_beat(b) for b in own_beats)
    ]


def pqtz_own_digest(beats: Sequence[Mapping[str, object]]) -> str:
    canonical = [
        {"n": int(b["n"]), "bpm": float(b["bpm"]), "t": float(b["t"])}
        for b in beats
    ]
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def anchor_count(payload: Mapping[str, object]) -> int:
    changes = payload.get("tempo_changes") or []
    return 1 if not changes else 1 + len(changes)


def is_fixed_tempo_payload(payload: Mapping[str, object]) -> bool:
    return not payload.get("tempo_changes")


def load_own_beatgrid(
    state_conn: sqlite3.Connection, stable_id: str
) -> Mapping[str, object] | None:
    pointer = canonical_pointer(state_conn, stable_id, "beatgrid")
    if pointer is None:
        return None
    row = state_conn.execute(
        "SELECT record_json FROM analysis "
        "WHERE stable_id = ? AND backend = ? AND backend_version = ?",
        (stable_id, pointer[0], pointer[1]),
    ).fetchone()
    if row is None:
        return None
    lane = AnalysisRecord.from_json(row[0]).lanes.get("beatgrid")
    if lane is None or lane.status != "ok":
        return None
    beats = lane.payload.get("beats") or []
    if not beats:
        return None
    return lane.payload


def resolve_analysis_dat_path(
    rb_conn: sqlite3.Connection, content_id: str
) -> Path | None:
    row = rb_conn.execute(
        "SELECT AnalysisDataPath FROM djmdContent WHERE ID = ?", (content_id,)
    ).fetchone()
    if row is None or not row[0]:
        return None
    raw = str(row[0])
    candidate = Path(raw)
    if candidate.is_file():
        return candidate.resolve()
    from apps.shared.platform_paths import resolve_asset_path

    mapped = resolve_asset_path(raw)
    if mapped.resolved is not None and mapped.resolved.is_file():
        return mapped.resolved
    return None


def _pqtz_entries(own_beats: Sequence[Mapping[str, object]]) -> list[Container]:
    return [
        Container(beat=n, tempo=tempo, time=time_ms)
        for n, tempo, time_ms in (quantize_own_beat(b) for b in own_beats)
    ]


def _make_pqtz_tag(entries: list[Container]) -> PQTZAnlzTag:
    content = Container(
        u1=None,
        u2=_PQTZ_U2,
        entry_count=len(entries),
        entries=entries,
    )
    len_tag = _PQTZ_LEN_HEADER + 8 * len(entries)
    tag_struct = Container(
        type="PQTZ",
        len_header=_PQTZ_LEN_HEADER,
        len_tag=len_tag,
        content=content,
    )
    return PQTZAnlzTag(structs.AnlzTag.build(tag_struct))


def _find_pqtz_tag(anlz: AnlzFile) -> PQTZAnlzTag | None:
    for tag in anlz.tags:
        if tag.type == "PQTZ":
            return tag
    return None


def build_minimal_dat(path: Path, seed_beats: Sequence[Mapping[str, object]]) -> None:
    """Write a PMAI file containing only a PQTZ tag (test helper)."""
    entries = _pqtz_entries(seed_beats)
    anlz = AnlzFile()
    anlz.file_header = Container(
        type="PMAI",
        len_header=_ANLZ_LEN_HEADER,
        len_file=0,
        u1=0,
        u2=0,
        u3=0,
        u4=0,
    )
    anlz.tags = [_make_pqtz_tag(entries)]
    anlz.update_len()
    path.parent.mkdir(parents=True, exist_ok=True)
    anlz.save(path)


def write_pqtz(dat_path: Path, own_beats: Sequence[Mapping[str, object]]) -> bool:
    if not dat_path.is_file():
        return False
    try:
        anlz = AnlzFile.parse_file(dat_path)
    except (OSError, ValueError):
        return False
    entries = _pqtz_entries(own_beats)
    pqtz = _find_pqtz_tag(anlz)
    if pqtz is None:
        anlz.tags.append(_make_pqtz_tag(entries))
    else:
        pqtz.content.entries = entries
        pqtz.content.entry_count = len(entries)
        pqtz.update_len()
    anlz.update_len()
    anlz.save(dat_path)
    return True


def verify_pqtz(dat_path: Path, own_beats: Sequence[Mapping[str, object]]) -> bool:
    if not dat_path.is_file():
        return False
    try:
        anlz = AnlzFile.parse_file(dat_path)
    except (OSError, ValueError):
        return False
    pqtz = _find_pqtz_tag(anlz)
    if pqtz is None:
        return False
    expected = served_beats_from_own(own_beats)
    actual = served_beats_from_pqtz_tag(pqtz)
    return actual == expected


def snapshot_pqtz_dat(dat_path: Path) -> dict[str, Any]:
    existed = dat_path.is_file()
    dat_bytes_b64: str | None = None
    if existed:
        dat_bytes_b64 = base64.b64encode(dat_path.read_bytes()).decode("ascii")
    return {
        "field": "pqtz",
        "dat_path": str(dat_path),
        "dat_bytes_b64": dat_bytes_b64,
        "existed": existed,
    }


def restore_pqtz_dat(snapshot: Mapping[str, Any]) -> None:
    dat_path = Path(str(snapshot["dat_path"]))
    if snapshot.get("existed"):
        raw = snapshot.get("dat_bytes_b64")
        if raw is None:
            raise RuntimeError(f"pqtz snapshot for {dat_path} missing preimage bytes")
        dat_path.write_bytes(base64.b64decode(str(raw)))
        return
    if dat_path.is_file():
        dat_path.unlink()


def write_pqtz_row(
    rb_conn: sqlite3.Connection,
    content_id: str,
    own_beats: Sequence[Mapping[str, object]],
    *,
    preimage: dict[str, Any],
) -> None:
    dat_path = resolve_analysis_dat_path(rb_conn, content_id)
    if dat_path is None:
        raise SafetyAbort(
            f"no AnalysisDataPath for content {content_id}"
        )
    preimage.update(snapshot_pqtz_dat(dat_path))
    if not write_pqtz(dat_path, own_beats):
        restore_pqtz_dat(preimage)
        raise SafetyAbort(f"write_pqtz failed for {dat_path}")
    if not verify_pqtz(dat_path, own_beats):
        restore_pqtz_dat(preimage)
        raise SafetyAbort(f"verify_pqtz failed for {dat_path}")


__all__ = [
    "PQTZ_TEMPO_SCALE",
    "PQTZ_TIME_SCALE",
    "SERVED_BPM_DECIMALS",
    "SERVED_T_DECIMALS",
    "anchor_count",
    "build_minimal_dat",
    "is_fixed_tempo_payload",
    "load_own_beatgrid",
    "pqtz_own_digest",
    "quantize_own_beat",
    "resolve_analysis_dat_path",
    "restore_pqtz_dat",
    "served_beats_from_own",
    "served_beats_from_pqtz_tag",
    "snapshot_pqtz_dat",
    "verify_pqtz",
    "write_pqtz",
    "write_pqtz_row",
]
