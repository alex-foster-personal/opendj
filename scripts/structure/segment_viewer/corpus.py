"""The segment viewer's data: one JSON document per track, the same for a human and an agent.

WHAT A TRACK DOCUMENT HOLDS (``track_segments``):

- ``grid``: the downbeats every lane is drawn and scored on. On the bench
  corpus that is All-In-One's own ``model_downbeats``, exactly as
  ``apps/analysis_structure/bench.py`` scores them, so a boundary the
  viewer shows is the boundary the bench measured.
- ``lanes``: ``own_fast``, ``allin1`` and ``pssi``, each with ``status``
  ``ok``, ``pending`` or ``unavailable`` and, unless ok, a ``reason``. A lane
  that cannot be measured is never filled with something else.
- ``agreement``: own-fast against All-In-One on that grid (boundary F at
  0.5/2/3 s, the 16-bar control, frame label agreement, drop F1), computed by
  the bench's own functions so the numbers match it.

WHERE THE INPUTS LIVE (``Corpus``): a directory holding ``corpus.jsonl``
(``{"stable_id", "file"}`` per line), ``allin1/<stable_id>.json`` (raw
``allin1_runner.py`` output) and ``peaks/<stable_id>.npy`` (3-band waveform
peaks, written there on first use by ``decode_peaks``), plus the directory of
audio files that ``file`` is relative to.

-Claude
"""

from __future__ import annotations

import bisect
import json
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from apps.analysis_structure.http import build_segment_bench
from apps.analysis_waveform.decode import DETAIL_COLUMNS_PER_S, decode_peaks

# The package's own algorithms, reached through its delivery entry point (ADR-NEW-module-contracts).
_BENCH = build_segment_bench()
GRID_ID = _BENCH.grid_id
TOLERANCES_S = _BENCH.tolerances_s
OWN_FAST_VERSION = _BENCH.own_fast_version
ALLIN1_VERSION = _BENCH.allin1_version
segment = _BENCH.segment
build_sidecar = _BENCH.build_sidecar
_inner = _BENCH.inner
_frames = _BENCH.frames
boundary_prf = _BENCH.boundary_prf
frame_counts = _BENCH.frame_counts
agreement_from_counts = _BENCH.agreement_from_counts
pooled_agreement = _BENCH.pooled_agreement

GRID_SOURCE = "allin1.model_downbeats"
PSSI_REASON = "no rekordbox analysis: bench corpus tracks are not in a rekordbox library"
SUMMARY_KEYS = ("f@0.5", "f@2.0", "f@3.0", "base_f@0.5", "base_f@2.0", "base_f@3.0")


class UnknownTrack(KeyError):
    """A stable id that is not in the corpus manifest."""


@dataclass(frozen=True)
class CorpusTrack:
    stable_id: str
    file: str


@dataclass
class Corpus:
    root: Path
    audio_dir: Path
    tracks: dict[str, CorpusTrack] = field(default_factory=dict)
    _cache: dict[str, tuple[tuple[float, float], dict[str, Any]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _track_locks: dict[str, threading.Lock] = field(default_factory=dict)

    @classmethod
    def open(cls, root: Path, audio_dir: Path) -> Corpus:
        manifest = root / "corpus.jsonl"
        if not manifest.is_file():
            raise FileNotFoundError(f"no corpus manifest at {manifest}")
        if not audio_dir.is_dir():
            raise FileNotFoundError(f"audio dir {audio_dir} does not exist")
        rows = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
        tracks = {r["stable_id"]: CorpusTrack(r["stable_id"], r["file"]) for r in rows}
        if len(tracks) != len(rows):
            raise ValueError(f"{manifest}: duplicate stable_id")
        return cls(root=root, audio_dir=audio_dir, tracks=tracks)

    # ----- paths -----------------------------------------------------------

    def track(self, stable_id: str) -> CorpusTrack:
        if stable_id not in self.tracks:
            raise UnknownTrack(stable_id)
        return self.tracks[stable_id]

    def audio_path(self, stable_id: str) -> Path:
        return self.audio_dir / self.track(stable_id).file

    def allin1_path(self, stable_id: str) -> Path:
        return self.root / "allin1" / f"{stable_id}.json"

    def peaks_path(self, stable_id: str) -> Path:
        return self.root / "peaks" / f"{stable_id}.npy"

    # ----- inputs ------------------------------------------------------------

    def peaks(self, stable_id: str) -> np.ndarray:
        """The 3-band peaks, decoded once from the audio and kept beside the corpus."""
        path = self.peaks_path(stable_id)
        if path.is_file():
            return np.load(path)
        audio = self.audio_path(stable_id)
        if not audio.is_file():
            raise FileNotFoundError(f"audio for {stable_id} missing at {audio}")
        peaks = decode_peaks(audio)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{stable_id}.tmp.npy")
        np.save(tmp, peaks)
        tmp.replace(path)
        return peaks

    def allin1_raw(self, stable_id: str) -> dict[str, Any] | None:
        path = self.allin1_path(stable_id)
        return json.loads(path.read_text()) if path.is_file() else None

    # ----- documents ---------------------------------------------------------

    def _stamp(self, stable_id: str) -> tuple[float, float]:
        a, p = self.allin1_path(stable_id), self.peaks_path(stable_id)
        return (a.stat().st_mtime if a.is_file() else 0.0, p.stat().st_mtime if p.is_file() else 0.0)

    def _cached(self, stable_id: str) -> dict[str, Any] | None:
        with self._lock:
            hit = self._cache.get(stable_id)
        return hit[1] if hit is not None and hit[0] == self._stamp(stable_id) else None

    def segments(self, stable_id: str) -> dict[str, Any]:
        """The track document, memoized until its All-In-One output or peaks change."""
        return _memoized_segments(self, stable_id, track_segments)

    def listing(self) -> list[dict[str, Any]]:
        """Every track with its All-In-One status and, once computed, its agreement."""
        out = []
        for sid, t in self.tracks.items():
            doc = self._cached(sid)
            out.append(
                {
                    "stable_id": sid,
                    "title": title_of(t.file),
                    "allin1": "ok" if self.allin1_path(sid).is_file() else "pending",
                    "agreement": doc["agreement"] if doc else None,
                    "duration_s": doc["duration_s"] if doc else None,
                }
            )
        return out

    def summary(self) -> dict[str, Any]:
        """Pooled means over the scored tracks, like the bench's SUMMARY line."""
        rows = [self.segments(sid)["agreement"] for sid in self.tracks if self.allin1_path(sid).is_file()]
        return summarize(len(self.tracks), rows)


def summarize(tracks: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The bench SUMMARY over agreement ``rows``: boundary F as track means, labels pooled by frames.

    ``label_acc`` and ``drop_f1`` pool every scored track's frame counts the way
    ``bench._summary`` does, so a long track keeps its weight and a track whose
    own drop F1 is undefined still adds its frames.
    """
    scored = [r for r in rows if r["status"] == "ok"]
    out: dict[str, Any] = {"tracks": tracks, "allin1_ready": len(rows), "scored": len(scored)}
    for k in SUMMARY_KEYS:
        vals = [r[k] for r in scored if r.get(k) is not None]
        out[k] = round(statistics.fmean(vals), 3) if vals else None
    out["label_acc"], out["drop_f1"], out["label_frames"] = pooled_agreement(scored, "counts")
    return out


# ----- pure helpers -----------------------------------------------------------


def title_of(file: str) -> str:
    """``Artist_-_Title.mp3`` as ``Artist - Title``."""
    return Path(file).stem.replace("_-_", " - ").replace("_", " ")


def bar_number(downbeats: list[float], t: float) -> int | None:
    """1-based bar holding time ``t``; None before the first downbeat (the pickup)."""
    idx = bisect.bisect_right(downbeats, t + 1e-3)
    return idx if idx > 0 else None


def _span(section: dict[str, Any], downbeats: list[float]) -> dict[str, Any]:
    return {
        "start_s": section["start_s"],
        "end_s": section["end_s"],
        "label": section.get("dj_label") or section["label"],
        "model_label": section["label"],
        "dj_label": section.get("dj_label"),
        "continuation": bool(section.get("continuation")),
        "start_bar": bar_number(downbeats, section["start_s"]),
        "end_bar": bar_number(downbeats, section["end_s"] - 1e-2),
        "energy_db": (section.get("energy") or {}).get("score_db"),
    }


def _moment_tag(moment: dict[str, Any]) -> str:
    kind = moment["kind"]
    if kind == "drop":
        return "D?" if moment.get("fake") else f"D{moment['number']}"
    if kind == "break":
        return "B"
    if kind == "build":
        return "Bu"
    raise ValueError(f"unknown moment kind {kind!r}")


def _lane_ok(
    sections: list[dict[str, Any]], moments: list[dict[str, Any]], downbeats: list[float], **extra: Any
) -> dict[str, Any]:
    return {
        "status": "ok",
        "reason": None,
        "sections": [_span(s, downbeats) for s in sections],
        "moments": [{**m, "tag": _moment_tag(m)} for m in moments],
        **extra,
    }


def _lane_off(status: str, reason: str) -> dict[str, Any]:
    return {"status": status, "reason": reason, "sections": [], "moments": []}


def _memoized_segments(
    owner: Any,
    stable_id: str,
    build: Callable[[Any, str], dict[str, Any]],
) -> dict[str, Any]:
    """Memoize ``build`` under a per-track lock until All-In-One output or peaks change."""
    owner.track(stable_id)
    with owner._lock:
        track_lock = owner._track_locks.setdefault(stable_id, threading.Lock())
    with track_lock:  # one decode per track, however many requests race for it
        doc = owner._cached(stable_id)
        if doc is not None:
            return doc
        doc = build(owner, stable_id)
        with owner._lock:
            owner._cache[stable_id] = (owner._stamp(stable_id), doc)
    return doc


def agreement(
    ref: list[dict[str, Any]], est: list[dict[str, Any]], downbeats: list[float], duration: float
) -> dict[str, Any]:
    """own-fast (est) against All-In-One (ref), with the bench's functions and edges."""
    ref_b = _inner([s["start_s"] for s in ref], duration)
    est_b = _inner([s["start_s"] for s in est], duration)
    base_b = _inner([float(t) for t in downbeats[16::16]], duration)
    row: dict[str, Any] = {"status": "ok", "reason": None, "ref_sections": len(ref), "est_sections": len(est)}
    for tol in TOLERANCES_S:
        row[f"f@{tol}"] = round(boundary_prf(ref_b, est_b, tol)[2], 3)
        row[f"base_f@{tol}"] = round(boundary_prf(ref_b, base_b, tol)[2], 3)
    counts = frame_counts(_frames(ref, duration), _frames(est, duration))
    acc, drop = agreement_from_counts(counts)
    row["counts"] = counts
    row["label_acc"] = round(acc, 3) if acc is not None else None
    row["drop_f1"] = round(drop, 3) if drop is not None else None
    return row


def _allin1_lane(
    stable_id: str, raw: dict[str, Any] | None, downbeats: list[float]
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    if raw is None:
        return _lane_off("pending", "All-In-One pending: no runner output for this track yet"), None
    if raw.get("status") != "ok":
        return _lane_off("unavailable", f"All-In-One runner failed: {raw.get('reason')}"), None
    doc = build_sidecar(stable_id, raw, downbeats, GRID_ID)
    if doc["status"] != "ok":
        return _lane_off("unavailable", f"All-In-One sidecar failed: {doc['reason']}"), None
    lane = _lane_ok(
        doc["sections"],
        doc.get("moments") or [],
        downbeats,
        producer_version=ALLIN1_VERSION,
        labels=doc["dj_labels"]["status"],
        labels_reason=doc["dj_labels"].get("reason"),
        runner_seconds=raw.get("seconds"),
    )
    return lane, doc["sections"]


def _own_fast_lane(
    peaks: np.ndarray, raw: dict[str, Any] | None, downbeats: list[float], duration: float
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    if not downbeats:
        status = "pending" if raw is None else "unavailable"
        return _lane_off(status, "no grid: this corpus's grid is All-In-One's downbeats, not ready yet"), None
    t0 = time.perf_counter()
    est = segment(peaks, downbeats, duration)
    ms = round((time.perf_counter() - t0) * 1000.0, 1)
    if est["status"] != "ok":
        return _lane_off("unavailable", f"own-fast failed: {est['reason']}"), None
    lane = _lane_ok(
        est["sections"],
        est["moments"],
        downbeats,
        producer_version=OWN_FAST_VERSION,
        labels=est["dj_labels"]["status"],
        labels_reason=est["dj_labels"].get("reason"),
        segment_ms=ms,
        phrase_offset=est["phrase_offset"],
    )
    return lane, est["sections"]


def track_segments(corpus: Corpus, stable_id: str) -> dict[str, Any]:
    track = corpus.track(stable_id)
    peaks = corpus.peaks(stable_id)
    duration = peaks.shape[0] / DETAIL_COLUMNS_PER_S
    raw = corpus.allin1_raw(stable_id)
    ok_raw = raw if raw is not None and raw.get("status") == "ok" else {}
    downbeats = [float(t) for t in ok_raw.get("model_downbeats") or []]
    beats = [float(t) for t in ok_raw.get("model_beats") or []]
    allin1, ref = _allin1_lane(stable_id, raw, downbeats)
    own_fast, est = _own_fast_lane(peaks, raw, downbeats, duration)
    if ref is not None and est is not None:
        agree = agreement(ref, est, downbeats, duration)
    else:
        agree = {"status": "unavailable", "reason": "needs both own-fast and All-In-One sections"}
    return {
        "stable_id": stable_id,
        "title": title_of(track.file),
        "file": track.file,
        "duration_s": round(duration, 3),
        "grid": {"source": GRID_SOURCE if downbeats else None, "downbeats": downbeats, "beats": beats},
        "peaks": {
            "url": f"/api/tracks/{stable_id}/peaks",
            "columns": int(peaks.shape[0]),
            "columns_per_s": DETAIL_COLUMNS_PER_S,
            "bands": ["low", "mid", "high"],
        },
        "audio": {"url": f"/api/tracks/{stable_id}/audio"},
        "lanes": {
            "own_fast": own_fast,
            "allin1": allin1,
            "pssi": _lane_off("unavailable", PSSI_REASON),
        },
        "agreement": agree,
    }


__all__ = ["Corpus", "UnknownTrack", "agreement", "bar_number", "title_of", "track_segments"]
