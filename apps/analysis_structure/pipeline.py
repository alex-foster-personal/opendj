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
``specs/native-analysis-v1.md``; promoting them into the record contract
means a new lane in ``apps.analysis.lane_enums``, its payload validator,
canonical pointer and projection. That lands once the lane has a bench score
on the parity reference (rekordbox PSSI). Until then the sidecar follows the
vocal-cache precedent: derived, machine-local, regenerable, merged into
``/anlz`` at serve time. `docs/decisions/ADR-NEW-structure-and-genre-sidecars.md`
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.analysis.canonical import canonical_pointer
from apps.analysis.record import AnalysisRecord

from .quantize import quantize

SCHEMA = "structure-sidecar/v1"
PRODUCER = "allin1"
PRODUCER_VERSION = "0.1.0"  # bump when the quantizer policy or runner pins change
RUNNER = Path(__file__).with_name("allin1_runner.py")
PHRASE_BARS = 4


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
    downbeats = [float(b["t"]) for b in lane.payload.get("beats", ()) if int(b.get("n", 0)) == 1]
    return downbeats, {"backend": pointer[0], "backend_version": pointer[1]}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _failed(stable_id: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "stable_id": stable_id,
        "status": "failed",
        "reason": reason,
        "producer": PRODUCER,
        "producer_version": PRODUCER_VERSION,
        "analyzed_at": _now(),
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
    q = quantize(segments, downbeats, phrase_bars=PHRASE_BARS)
    sections = q.sections(duration_s)
    if not sections:
        return _failed(stable_id, "no_boundaries", versions=raw.get("versions"))
    return {
        "schema": SCHEMA,
        "stable_id": stable_id,
        "status": "ok",
        "reason": None,
        "producer": PRODUCER,
        "producer_version": PRODUCER_VERSION,
        "versions": raw.get("versions"),
        "analyzed_at": _now(),
        "grid": {**grid, "downbeats": len(downbeats)},
        "duration_s": round(duration_s, 3),
        "sections": sections,
        "quantize": q.to_dict(),
        "model_segments": raw.get("segments"),
        "seconds": raw.get("seconds"),
    }


def write_sidecar(data_dir: Path, doc: dict[str, Any]) -> Path:
    path = sidecar_path(data_dir, doc["stable_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, path)
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


__all__ = [
    "PRODUCER",
    "PRODUCER_VERSION",
    "SCHEMA",
    "analyze",
    "build_sidecar",
    "cache_dir",
    "load_sidecar",
    "own_downbeats",
    "sidecar_path",
    "write_sidecar",
]
