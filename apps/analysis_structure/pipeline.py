"""Run All-In-One over library tracks, quantize onto the own beatgrid, write sidecars.

THE ORDER OF OPERATIONS. For each track: read the canonical own ``beatgrid``
record (its downbeats are the only grid this lane snaps to), resolve this
machine's local audio, hand both to the out-of-process runner
(``allin1_runner.py``, heavy deps in its own PEP 723 environment), then
quantize in the app venv and write
``<data>/state/structure-cache/<stable_id>.json``.

A TRACK THAT CANNOT BE MEASURED STILL GETS A FILE. No own beatgrid, no local
audio, and a runner failure each write ``status: failed`` with a named
reason, so "not analyzed yet" and "could not be analyzed" never look alike
(STRUCT-01). Nothing is ever written with ``status: ok`` and no sections.

WHY A SIDECAR AND NOT A LANE ROW. Phrases are a v2 lane in
``(private path)``; promoting them into the record contract
means a new lane in ``apps.analysis.lane_enums``, its payload validator,
canonical pointer and projection. That lands once the lane has a bench score
on the parity reference (rekordbox PSSI). Until then the sidecar follows the
vocal-cache precedent: derived, machine-local, regenerable, merged into
``/anlz`` at serve time. `(private path)`
records the decision.

-Claude
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from apps.analysis.canonical import canonical_pointer
from apps.analysis.record import AnalysisRecord

from ._beatgrid import downbeats_from_beats
from ._io import atomic_write_json, utc_now_iso_seconds
from . import dj_labels
from .dj_labels import label_sections
from .drop_events import mark_drops
from .quantize import TIME_S_DECIMALS, quantize

SCHEMA = "structure-sidecar/v1"
PRODUCER = "allin1"
PRODUCER_VERSION = "0.2.0"  # bump when the quantizer, the DJ label rules or runner pins change
RUNNER = Path(__file__).with_name("allin1_runner.py")
PHRASE_BARS = 4
# Boundaries snap to downbeats, and phrase alignment is REPORTED, not forced:
# on the SALAMI spike (4 tracks, annotator ceiling F@0.5 0.60) the downbeat
# snap lifted All-In-One from 0.44 to 0.47, and moving a further bar onto a
# 4-bar phrase dropped it to 0.29. Raise this only once phrase snapping beats
# the downbeat snap against rekordbox PSSI on dance music.
PHRASE_MAX_MOVE_BARS = 0


def cache_dir(data_dir: Path) -> Path:
    return data_dir / "state" / "structure-cache"


def sidecar_path(data_dir: Path, stable_id: str) -> Path:
    return cache_dir(data_dir) / f"{stable_id}.json"


def own_downbeats(
    conn: sqlite3.Connection, stable_id: str
) -> tuple[list[float], dict[str, str]] | None:
    """Downbeat times of the canonical own beatgrid, plus which record they came from."""
    pointer = canonical_pointer(conn, stable_id, "beatgrid")
    if pointer is None:
        return None
    row = conn.execute(
        "SELECT record_json FROM analysis "
        "WHERE stable_id = ? AND backend = ? AND backend_version = ?",
        (stable_id, pointer[0], pointer[1]),
    ).fetchone()
    if row is None:
        return None
    lane = AnalysisRecord.from_json(row[0]).lanes.get("beatgrid")
    if lane is None or lane.status != "ok":
        return None
    downbeats = downbeats_from_beats(lane.payload.get("beats", ()))
    return downbeats, {"backend": pointer[0], "backend_version": pointer[1]}


def _failed(stable_id: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "stable_id": stable_id,
        "status": "failed",
        "reason": reason,
        "producer": PRODUCER,
        "producer_version": PRODUCER_VERSION,
        "analyzed_at": utc_now_iso_seconds(),
        **extra,
    }


def build_sidecar(
    stable_id: str,
    raw: dict[str, Any],
    downbeats: Sequence[float],
    grid: dict[str, str],
) -> dict[str, Any]:
    """Runner output + own downbeats -> the sidecar document. Pure."""
    if raw.get("status") != "ok":
        return _failed(
            stable_id, "runner_failed", detail=raw.get("reason"), versions=raw.get("versions")
        )
    segments = [(s["start"], s["end"], s["label"]) for s in raw.get("segments", [])]
    if not segments:
        return _failed(stable_id, "no_segments", versions=raw.get("versions"))
    duration_s = max(s[1] for s in segments)
    q = quantize(
        segments, downbeats, phrase_bars=PHRASE_BARS, max_phrase_move_bars=PHRASE_MAX_MOVE_BARS
    )
    sections = q.sections(duration_s)
    if not sections:
        return _failed(stable_id, "no_boundaries", versions=raw.get("versions"))
    dj = attach_dj_labels(sections, raw)
    moments = mark_drops(sections) if dj["status"] == "ok" else []
    return {
        "schema": SCHEMA,
        "stable_id": stable_id,
        "status": "ok",
        "reason": None,
        "producer": PRODUCER,
        "producer_version": PRODUCER_VERSION,
        "versions": raw.get("versions"),
        "analyzed_at": utc_now_iso_seconds(),
        "grid": {**grid, "downbeats": len(downbeats)},
        "duration_s": round(duration_s, TIME_S_DECIMALS),
        "sections": sections,
        "quantize": q.to_dict(),
        "dj_labels": dj,
        "moments": moments,
        "model_segments": raw.get("segments"),
        "seconds": raw.get("seconds"),
    }


def attach_dj_labels(sections: list[dict[str, Any]], raw: dict[str, Any]) -> dict[str, Any]:
    """Add ``dj_label``/``ambient``/``energy`` to each section in place; return the summary.

    The model's ``label`` is kept untouched. With no envelopes, or no energy
    contrast, the sections stay unlabeled and the summary names why
    (``dj_labels.py`` has the rules).
    """
    result = label_sections(sections, raw.get("envelopes"))
    if result["status"] != "ok":
        detail = raw.get("envelopes_error")
        return {**result, "detail": detail} if detail else result
    for section, dj in zip(sections, result["sections"], strict=True):
        section.update(dj)
    return {k: v for k, v in result.items() if k != "sections"}


def write_sidecar(data_dir: Path, doc: dict[str, Any]) -> Path:
    path = sidecar_path(data_dir, doc["stable_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, doc)
    return path


def load_sidecar(data_dir: Path, stable_id: str) -> dict[str, Any] | None:
    path = sidecar_path(data_dir, stable_id)
    if not path.is_file():
        return None
    doc = json.loads(path.read_text())
    if doc.get("schema") != SCHEMA:
        raise ValueError(f"{path}: schema {doc.get('schema')!r} is not {SCHEMA}")
    return doc


def run_runner(manifest: list[dict[str, str]], out_dir: Path, device: str) -> None:
    """Invoke the PEP 723 runner; its per-track JSON lands in ``out_dir``."""
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write("\n".join(json.dumps(m) for m in manifest))
        manifest_path = fh.name
    try:
        subprocess.run(
            [
                "uv",
                "run",
                "--no-project",
                "--script",
                str(RUNNER),
                "--manifest",
                manifest_path,
                "--out",
                str(out_dir),
                "--device",
                device,
            ],
            check=True,
        )
    finally:
        os.unlink(manifest_path)


def analyze(
    conn: sqlite3.Connection,
    stable_ids: Iterable[str],
    audio_paths: dict[str, Path | None],
    data_dir: Path,
    *,
    device: str = "auto",
) -> list[dict[str, Any]]:
    """Analyze ``stable_ids``; every one of them ends with a sidecar, ok or failed."""
    docs: list[dict[str, Any]] = []
    todo: list[tuple[str, list[float], dict[str, str], Path]] = []
    for sid in stable_ids:
        own = own_downbeats(conn, sid)
        path = audio_paths.get(sid)
        if own is None or len(own[0]) < 2:
            docs.append(write_and_return(data_dir, _failed(sid, "no_own_beatgrid")))
        elif path is None:
            docs.append(write_and_return(data_dir, _failed(sid, "audio_not_local")))
        else:
            todo.append((sid, own[0], own[1], path))
    if todo:
        with tempfile.TemporaryDirectory(prefix="structure-") as tmp:
            out = Path(tmp)
            run_runner([{"stable_id": s, "path": str(p)} for s, _d, _g, p in todo], out, device)
            for sid, downbeats, grid, _path in todo:
                raw_path = out / f"{sid}.json"
                raw = (
                    json.loads(raw_path.read_text())
                    if raw_path.is_file()
                    else {"status": "failed", "reason": "runner wrote no output"}
                )
                docs.append(write_and_return(data_dir, build_sidecar(sid, raw, downbeats, grid)))
    return docs


def write_and_return(data_dir: Path, doc: dict[str, Any]) -> dict[str, Any]:
    write_sidecar(data_dir, doc)
    return doc


NICHE_LABELS_FIELD = "niche_labels"
NICHE_STATE_V = 1
NICHE_PROVIDER = "structure-niche-v1"
SUGGESTION_ACCEPTED = "accepted"
SUGGESTION_REJECTED = "rejected"


def empty_niche_state() -> dict[str, Any]:
    return {"v": NICHE_STATE_V, "regions": [], "suggestion_state": {}}


def user_region_id(start_s: float, end_s: float, label: str) -> str:
    return f"user:{start_s}:{end_s}:{label}"


def normalize_niche_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("v") != NICHE_STATE_V:
        return empty_niche_state()
    out: dict[str, Any] = {
        "v": NICHE_STATE_V,
        "regions": [],
        "suggestion_state": {},
    }
    regions = raw.get("regions")
    if isinstance(regions, list):
        for row in regions:
            if not isinstance(row, dict):
                continue
            label = row.get("label")
            if label is None:
                continue
            start_s, end_s = row.get("start_s"), row.get("end_s")
            if start_s is None or end_s is None:
                continue
            region_id = row.get("id") or user_region_id(float(start_s), float(end_s), str(label))
            out["regions"].append(
                {
                    "id": str(region_id),
                    "start_s": float(start_s),
                    "end_s": float(end_s),
                    "label": str(label),
                    "source": "user",
                }
            )
    verdicts = raw.get("suggestion_state")
    if isinstance(verdicts, dict):
        for sid, verdict in verdicts.items():
            if verdict in (SUGGESTION_ACCEPTED, SUGGESTION_REJECTED):
                out["suggestion_state"][str(sid)] = verdict
    return out


def read_niche_state(conn: sqlite3.Connection, stable_id: str) -> dict[str, Any]:
    from apps.shared.state import provenance

    row = provenance.read_field(conn, stable_id, NICHE_LABELS_FIELD)
    if row is None:
        return empty_niche_state()
    return normalize_niche_state(row.value)


def record_user_region(
    state: dict[str, Any], *, start_s: float, end_s: float, label: str
) -> dict[str, Any]:
    if end_s <= start_s:
        raise ValueError("end_s must be after start_s")
    label = label.strip()
    if not label:
        raise ValueError("label must be non-empty")
    next_state = normalize_niche_state(state)
    region = {
        "id": user_region_id(start_s, end_s, label),
        "start_s": round(float(start_s), 3),
        "end_s": round(float(end_s), 3),
        "label": label,
        "source": "user",
    }
    regions = [r for r in next_state["regions"] if r["id"] != region["id"]]
    regions.append(region)
    next_state["regions"] = sorted(regions, key=lambda r: (r["start_s"], r["id"]))
    return next_state


def set_suggestion_verdict(
    state: dict[str, Any], suggestion_id: str, *, accepted: bool
) -> dict[str, Any]:
    next_state = normalize_niche_state(state)
    next_state["suggestion_state"][suggestion_id] = (
        SUGGESTION_ACCEPTED if accepted else SUGGESTION_REJECTED
    )
    return next_state


def niche_suggestions_for_track(data_dir: Path, stable_id: str) -> list[dict[str, Any]]:
    doc = load_sidecar(data_dir, stable_id)
    if doc is None or doc.get("status") != "ok":
        return []
    return dj_labels.structure_niche_suggestions(doc.get("sections") or [], doc.get("dj_labels") or {})


def suggestions_with_verdicts(
    suggestions: list[dict[str, Any]], state: dict[str, Any]
) -> list[dict[str, Any]]:
    verdicts = normalize_niche_state(state)["suggestion_state"]
    out: list[dict[str, Any]] = []
    for row in suggestions:
        sid = row["suggestion_id"]
        verdict = verdicts.get(sid)
        out.append(
            {
                **row,
                "verdict": verdict,
                "accepted": verdict == SUGGESTION_ACCEPTED,
            }
        )
    return out


def accepted_query_labels(
    state: dict[str, Any], suggestions: list[dict[str, Any]]
) -> list[str]:
    """Labels that may drive a niche library search (issue #11190)."""
    norm = normalize_niche_state(state)
    labels: list[str] = []
    seen: set[str] = set()
    for region in norm["regions"]:
        if region["label"] not in seen:
            labels.append(region["label"])
            seen.add(region["label"])
    accepted_ids = {
        sid for sid, v in norm["suggestion_state"].items() if v == SUGGESTION_ACCEPTED
    }
    for row in suggestions:
        if row["suggestion_id"] in accepted_ids and row["label"] not in seen:
            labels.append(row["label"])
            seen.add(row["label"])
    return labels


def _sections_with_label(
    doc: dict[str, Any], label: str
) -> list[tuple[float, float, str, str]]:
    if doc.get("status") != "ok":
        return []
    hits: list[tuple[float, float, str, str]] = []
    for section in doc.get("sections") or []:
        dj_label = section.get("dj_label")
        if dj_label == label:
            hits.append(
                (float(section["start_s"]), float(section["end_s"]), str(dj_label), "structure")
            )
    return hits


def _user_regions_with_label(state: dict[str, Any], label: str) -> list[tuple[float, float, str, str]]:
    return [
        (r["start_s"], r["end_s"], r["label"], "user")
        for r in normalize_niche_state(state)["regions"]
        if r["label"] == label
    ]


def search_niche_library(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    source_stable_id: str,
    query_labels: Sequence[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Tracks that share an accepted niche label; never embedding cosine neighbours."""
    if not query_labels:
        return []
    labels = list(dict.fromkeys(str(label) for label in query_labels))
    rows = conn.execute(
        "SELECT stable_id FROM tracks WHERE deleted_at IS NULL AND stable_id != ?",
        (source_stable_id,),
    ).fetchall()
    hits: list[dict[str, Any]] = []
    for (stable_id,) in rows:
        doc = load_sidecar(data_dir, stable_id)
        state = read_niche_state(conn, stable_id)
        citations: list[dict[str, Any]] = []
        for label in labels:
            for start_s, end_s, cite_label, source in (
                *_sections_with_label(doc or {}, label),
                *_user_regions_with_label(state, label),
            ):
                citations.append(
                    {
                        "start_s": round(float(start_s), 3),
                        "end_s": round(float(end_s), 3),
                        "label": cite_label,
                        "source": source,
                    }
                )
        if not citations:
            continue
        hits.append(
            {
                "stable_id": stable_id,
                "score": float(len(citations)),
                "citations": citations,
            }
        )
    hits.sort(key=lambda row: (-row["score"], row["stable_id"]))
    return hits[:limit]


def niche_find_similar(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    stable_id: str,
    limit: int,
    query_labels: Sequence[str] | None = None,
) -> dict[str, Any]:
    state = read_niche_state(conn, stable_id)
    suggestions = niche_suggestions_for_track(data_dir, stable_id)
    labels = list(query_labels) if query_labels is not None else accepted_query_labels(state, suggestions)
    return {
        "stable_id": stable_id,
        "mode": "niche",
        "provider": NICHE_PROVIDER,
        "query_labels": labels,
        "candidates": search_niche_library(
            conn, data_dir, source_stable_id=stable_id, query_labels=labels, limit=limit
        ),
    }


__all__ = [
    "PRODUCER",
    "PRODUCER_VERSION",
    "SCHEMA",
    "NICHE_LABELS_FIELD",
    "NICHE_PROVIDER",
    "accepted_query_labels",
    "analyze",
    "attach_dj_labels",
    "build_sidecar",
    "cache_dir",
    "empty_niche_state",
    "load_sidecar",
    "niche_find_similar",
    "niche_suggestions_for_track",
    "normalize_niche_state",
    "own_downbeats",
    "read_niche_state",
    "record_user_region",
    "search_niche_library",
    "set_suggestion_verdict",
    "sidecar_path",
    "suggestions_with_verdicts",
    "write_sidecar",
]
