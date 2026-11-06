"""Library mode: any track of the real library, scored against its rekordbox phrases (PSSI).

WHAT IT READS, ALL READ-ONLY. The app's data folder (``--library``): the
decrypted ``master.plain.db`` and ``state/state.db``, opened ``immutable`` so
nothing (not even a ``-shm`` file) is written beside them. A track's rekordbox
beatgrid (PQTZ) and phrases (PSSI) come from its ANLZ files through the same
functions ``GET /anlz`` serves them with (``rb_vendor_pkg.anlz``), moved onto
our decode timeline by the same MP3 lead-in shift. Audio is the track's own
file on this machine, never copied anywhere by the viewer.

WHAT IT WRITES. Only under ``--work`` (gitignored, in the primary checkout's
``(private path)``): ``peaks/<stable_id>.npy`` decoded by
``apps.analysis_waveform.decode``, keyed on the audio file's size and mtime,
the decoder that will run and the decode profile's ``peaks_version``.
All-In-One output for the bench set is read from ``allin1/<stable_id>.json``;
``bench.json`` lists the bench ids (ids only).

THE GRID. Every lane is cut and scored on rekordbox's own downbeats (PQTZ
``n == 1``), the grid the PSSI phrases sit on and the one the deck draws by
default. Own-fast runs on it exactly as ``fast_sidecar`` does at serve time,
All-In-One is quantized onto it with ``pipeline.build_sidecar``.

THE SCORE (``against_rekordbox``). rekordbox phrases are the reference.
Boundary F with the bench's one-to-one matcher at 1 and 2 bars of this track's
tempo and at 0.5 s and 3.0 s (the parity phrase scorer's bands), each beside
the 16-bar control. Labels compare per 0.5 s frame after mapping both sides to
one vocabulary (``PSSI_DJ``): only phrase names with a stated DJ meaning map
(mood-high Up/Down, and Intro/Chorus/Outro in every mood); Verse and Bridge do
not, so their frames are left out and the mapped share is reported.

-Claude
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from apps.analysis_structure.http import build_segment_bench
from apps.analysis_waveform.decode import DETAIL_COLUMNS_PER_S, decode_peaks_from, select_decoder
from apps.analysis_waveform.local_waveform import peaks_version

from .corpus import _lane_off, _lane_ok, _memoized_segments

# The package's own algorithms, reached through its delivery entry point (ADR-NEW-module-contracts).
_BENCH = build_segment_bench()
OWN_FAST_VERSION = _BENCH.own_fast_version
ALLIN1_VERSION = _BENCH.allin1_version
segment = _BENCH.segment
build_sidecar = _BENCH.build_sidecar
_inner = _BENCH.inner
_frames = _BENCH.frames
boundary_prf = _BENCH.boundary_prf
frame_counts = _BENCH.frame_counts
agreement_from_counts = _BENCH.agreement_from_counts

GRID_ID = {"backend": "rekordbox.pqtz", "backend_version": "0"}
GRID_SOURCE = "rekordbox.pqtz"
BAR_TOLERANCES = (1, 2)
SECOND_TOLERANCES = (0.5, 3.0)
CONTROL_BARS = 16
PAIRED_KEYS = ("f@1bar", "f@2bar", "base_f@1bar", "base_f@2bar", "label_acc")
SCORED_LANES = ("own_fast", "allin1")
SUMMARY_KEYS = ("f@1bar", "f@2bar", "f@0.5s", "f@3.0s", "base_f@1bar", "base_f@2bar", "base_f@0.5s", "base_f@3.0s")

# rekordbox phrase names per PSSI mood, as apps/webui/frontend/src/lib/components/rb/wave/sections.ts
# (crate-digger rekordbox_anlz.ksy enums; low mood shows three ids each as Verse 1 / Verse 2).
PSSI_NAMES: dict[int, dict[int, str]] = {
    1: {1: "Intro", 2: "Up", 3: "Down", 5: "Chorus", 6: "Outro"},
    2: {
        1: "Intro",
        2: "Verse 1",
        3: "Verse 2",
        4: "Verse 3",
        5: "Verse 4",
        6: "Verse 5",
        7: "Verse 6",
        8: "Bridge",
        9: "Chorus",
        10: "Outro",
    },
    3: {
        1: "Intro",
        2: "Verse 1",
        3: "Verse 1",
        4: "Verse 1",
        5: "Verse 2",
        6: "Verse 2",
        7: "Verse 2",
        8: "Bridge",
        9: "Chorus",
        10: "Outro",
    },
}
MOOD_NAMES = {1: "high", 2: "mid", 3: "low"}
# The mapping (private path) states:
# Up -> buildup, Down -> breakdown, Chorus -> drop. Verse and Bridge carry no DJ meaning.
PSSI_DJ = {"Intro": "intro", "Up": "buildup", "Down": "breakdown", "Chorus": "drop", "Outro": "outro"}
# Our DJ labels onto the same vocabulary: rekordbox has one "Down" for both calm kinds.
PSSI_DJ_IDENTITY = {v: v for v in PSSI_DJ.values()}
OWN_DJ = {
    "intro": "intro",
    "buildup": "buildup",
    "drop": "drop",
    "breakdown": "breakdown",
    "cooldown": "breakdown",
    "outro": "outro",
}

PathResolver = Callable[[str], Path | None]


class UnknownTrack(KeyError):
    """A stable id that is not a live rekordbox-mapped library track."""


class RekordboxUnavailable(RuntimeError):
    """The track has no readable rekordbox analysis (no ANLZ, no grid)."""


@dataclass(frozen=True)
class LibraryTrack:
    stable_id: str
    vendor_id: str
    title: str
    artist: str
    genre: str | None
    bpm: float | None
    length_s: int | None
    folder_path: str | None
    anlz_path: str | None
    audio_candidates: tuple[str, ...]


# ----- index ---------------------------------------------------------------------


def _open_immutable(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"required database missing: {path}")
    conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _read_index(master_db: Path, state_db: Path) -> dict[str, LibraryTrack]:
    state = _open_immutable(state_db)
    try:
        mapped = state.execute(
            "SELECT v.vendor_id, t.stable_id, t.file_path FROM track_vendor_ids v JOIN tracks t USING (stable_id) "
            "WHERE v.vendor = 'rekordbox' AND v.deleted_at IS NULL AND t.deleted_at IS NULL"
        ).fetchall()
        locs: dict[str, list[str]] = {}
        for sid, path in state.execute(
            "SELECT stable_id, file_path FROM track_locations "
            "WHERE kind = 'local' AND deleted_at IS NULL AND file_path IS NOT NULL ORDER BY role = 'primary' DESC"
        ):
            locs.setdefault(sid, []).append(path)
    finally:
        state.close()
    by_vendor = {str(v): (sid, fp) for v, sid, fp in mapped}
    master = _open_immutable(master_db)
    try:
        rows = master.execute(
            "SELECT c.ID, c.Title, a.Name, g.Name, c.BPM, c.Length, c.FolderPath, c.AnalysisDataPath "
            "FROM djmdContent c "
            "LEFT JOIN djmdArtist a ON a.ID = c.ArtistID "
            "LEFT JOIN djmdGenre g ON g.ID = c.GenreID "
            "WHERE c.rb_local_deleted = 0"
        ).fetchall()
    finally:
        master.close()
    out: dict[str, LibraryTrack] = {}
    for vid, title, artist, genre, bpm, length, folder, anlz in rows:
        hit = by_vendor.get(str(vid))
        if hit is None:
            continue
        sid, state_path = hit
        candidates = tuple(dict.fromkeys(p for p in (state_path, *locs.get(sid, ())) if p))
        out[sid] = LibraryTrack(
            stable_id=sid,
            vendor_id=str(vid),
            title=title or "",
            artist=artist or "",
            genre=genre or None,
            bpm=round(bpm / 100.0, 2) if bpm else None,
            length_s=int(length) if length is not None else None,
            folder_path=folder or None,
            anlz_path=anlz or None,
            audio_candidates=candidates,
        )
    return out


def default_resolver(path: str) -> Path | None:
    """rekordbox's stored path mapped onto this machine (share root, path map), or None."""
    from apps.adapters.rekordbox.paths import resolve_asset_path

    return resolve_asset_path(path).resolved


# ----- rekordbox analysis ----------------------------------------------------------


def read_rekordbox(anlz_dat: Path, folder_path: str | None) -> dict[str, Any]:
    """PQTZ beats and PSSI phrases on our timeline, through the functions /anlz uses."""
    from apps.webui.server.rb_vendor_pkg.anlz import _first_tags, _phrases_payload
    from apps.webui.server.rb_vendor_pkg.lead_in_shift import anlz_lead_in_s, on_our_timeline
    from apps.webui.server.rb_vendor_pkg.own_beatgrid_overlay import _beatgrid_payload

    tags, unreadable = _first_tags(anlz_dat.parent)
    grid, times = _beatgrid_payload(tags)
    pssi = tags.get("PSSI")
    mood = int(pssi.content.mood) if pssi is not None else None
    payload: dict[str, Any] = {"beatgrid": grid, "phrases": _phrases_payload(tags, times)}
    lead_in = anlz_lead_in_s(folder_path)
    payload = on_our_timeline(payload, lead_in)
    return {
        "beats": payload["beatgrid"]["beats"],
        "phrases": payload["phrases"],
        "mood": mood,
        "lead_in_s": lead_in,
        "unreadable": unreadable,
    }


def pssi_sections(phrases: list[dict[str, Any]], mood: int | None) -> list[dict[str, Any]]:
    """PSSI phrases as viewer sections: ``label`` the rekordbox name, ``dj_label`` its DJ meaning or None."""
    names = PSSI_NAMES.get(mood or 0, {})
    out = []
    for p in phrases:
        if p["end_s"] <= p["start_s"]:
            continue
        name = names.get(int(p["kind"]), f"kind {p['kind']}")
        out.append(
            {
                "start_s": p["start_s"],
                "end_s": p["end_s"],
                "label": name,
                "dj_label": PSSI_DJ.get(name),
                "kind": int(p["kind"]),
            }
        )
    return out


# ----- scoring ----------------------------------------------------------------------


def _bar_s(downbeats: list[float]) -> float:
    return float(statistics.median(np.diff(downbeats))) if len(downbeats) > 1 else 0.0


def _family_frames(sections: list[dict[str, Any]], table: dict[str, str], duration: float) -> list[str | None]:
    mapped = [{**s, "dj_label": table.get(s.get("dj_label") or "")} for s in sections]
    return _frames(mapped, duration)


def against_rekordbox(
    ref: list[dict[str, Any]], est: list[dict[str, Any]], downbeats: list[float], duration: float
) -> dict[str, Any]:
    """One lane (est) against rekordbox's phrases (ref), with the bench's matcher and frames."""
    ref_b = _inner([s["start_s"] for s in ref], duration)
    est_b = _inner([s["start_s"] for s in est], duration)
    base_b = _inner([float(t) for t in downbeats[CONTROL_BARS::CONTROL_BARS]], duration)
    bar_s = _bar_s(downbeats)
    row: dict[str, Any] = {
        "status": "ok",
        "reason": None,
        "ref_sections": len(ref),
        "est_sections": len(est),
        "bar_s": round(bar_s, 4),
    }
    tols = [(f"{k}bar", k * bar_s) for k in BAR_TOLERANCES] + [(f"{t}s", t) for t in SECOND_TOLERANCES]
    for name, tol in tols:
        row[f"f@{name}"] = round(boundary_prf(ref_b, est_b, tol)[2], 3)
        row[f"base_f@{name}"] = round(boundary_prf(ref_b, base_b, tol)[2], 3)
    ref_f = _family_frames(ref, PSSI_DJ_IDENTITY, duration)
    est_f = _family_frames(est, OWN_DJ, duration)
    counts = frame_counts(ref_f, est_f)
    acc, drop = agreement_from_counts(counts)
    row["label_acc"] = round(acc, 3) if acc is not None else None
    row["drop_f1"] = round(drop, 3) if drop is not None else None
    row["frames"] = counts
    row["ref_mapped_frames"] = sum(1 for f in ref_f if f)
    row["ref_frames"] = len(ref_f)
    return row


# ----- the library --------------------------------------------------------------------


@dataclass
class Library:
    root: Path
    work: Path
    tracks: dict[str, LibraryTrack]
    resolve: PathResolver
    _cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _track_locks: dict[str, threading.Lock] = field(default_factory=dict)

    @classmethod
    def open(cls, root: Path, work: Path, resolve: PathResolver | None = None) -> Library:
        """``resolve`` maps a stored path onto this machine; None is ``default_resolver``, looked up now."""
        tracks = _read_index(root / "master.plain.db", root / "state" / "state.db")
        if not tracks:
            raise ValueError(f"{root}: no rekordbox-mapped tracks in the library")
        work.mkdir(parents=True, exist_ok=True)
        return cls(root=root, work=work, tracks=tracks, resolve=resolve or default_resolver)

    # ----- paths -------------------------------------------------------------------

    def track(self, stable_id: str) -> LibraryTrack:
        if stable_id not in self.tracks:
            raise UnknownTrack(stable_id)
        return self.tracks[stable_id]

    def audio_path(self, stable_id: str) -> Path:
        """The first candidate path that is a file here: state.db's path, its local locations, then rekordbox's."""
        t = self.track(stable_id)
        for raw in (*t.audio_candidates, t.folder_path):
            if not raw:
                continue
            p = self.resolve(raw)
            if p is not None and p.is_file():
                return p
        raise FileNotFoundError(f"no audio file on this machine for {stable_id}")

    def anlz_dat(self, stable_id: str) -> Path:
        t = self.track(stable_id)
        if not t.anlz_path:
            raise RekordboxUnavailable("rekordbox has no AnalysisDataPath for this track")
        p = self.resolve(t.anlz_path)
        if p is None or not p.is_file():
            raise RekordboxUnavailable("the track's ANLZ file is not on this machine")
        return p

    def peaks_path(self, stable_id: str) -> Path:
        return self.work / "peaks" / f"{stable_id}.npy"

    def allin1_path(self, stable_id: str) -> Path:
        return self.work / "allin1" / f"{stable_id}.json"

    def bench_path(self) -> Path:
        return self.work / "bench.json"

    # ----- inputs ------------------------------------------------------------------

    def peaks(self, stable_id: str) -> np.ndarray:
        """3-band peaks of the track's audio, decoded once and kept under ``work/peaks``."""
        audio = self.audio_path(stable_id)
        st = audio.stat()
        path, meta = self.peaks_path(stable_id), self.peaks_path(stable_id).with_suffix(".json")
        decoder = select_decoder(audio)
        # Decoder and profile identity are part of what the peaks MEAN (decode docstring).
        key = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "decoder": decoder, "peaks_version": peaks_version()}
        if path.is_file() and meta.is_file() and json.loads(meta.read_text(encoding="utf-8")) == key:
            return np.load(path)
        peaks = decode_peaks_from(decoder, audio)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{stable_id}.tmp.npy")
        np.save(tmp, peaks)
        tmp.replace(path)
        meta.write_text(json.dumps(key), encoding="utf-8", newline="\n")
        return peaks

    def allin1_raw(self, stable_id: str) -> dict[str, Any] | None:
        path = self.allin1_path(stable_id)
        return json.loads(path.read_text()) if path.is_file() else None

    def bench_ids(self) -> list[str]:
        path = self.bench_path()
        if not path.is_file():
            return []
        return list(json.loads(path.read_text())["stable_ids"])

    # ----- documents ---------------------------------------------------------------

    def _stamp(self, stable_id: str) -> tuple[Any, ...]:
        a, p = self.allin1_path(stable_id), self.peaks_path(stable_id)
        return (a.stat().st_mtime if a.is_file() else 0.0, p.stat().st_mtime if p.is_file() else 0.0)

    def _cached(self, stable_id: str) -> dict[str, Any] | None:
        with self._lock:
            hit = self._cache.get(stable_id)
        return hit[1] if hit is not None and hit[0] == self._stamp(stable_id) else None

    def segments(self, stable_id: str) -> dict[str, Any]:
        return _memoized_segments(self, stable_id, library_segments)

    def row(self, stable_id: str) -> dict[str, Any]:
        t = self.track(stable_id)
        doc = self._cached(stable_id)
        return {
            "stable_id": stable_id,
            "title": f"{t.artist} - {t.title}" if t.artist else t.title,
            "genre": t.genre,
            "bpm": t.bpm,
            "length_s": t.length_s,
            "has_anlz": bool(t.anlz_path),
            "allin1": "ok" if self.allin1_path(stable_id).is_file() else "pending",
            "scores": doc["scores"] if doc else None,
        }

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Tracks whose artist, title or genre holds every word of ``query`` (case-insensitive)."""
        words = query.lower().split()
        hits = []
        for sid, t in self.tracks.items():
            hay = f"{t.artist} {t.title} {t.genre or ''}".lower()
            if all(w in hay for w in words):
                hits.append(sid)
                if len(hits) >= limit:
                    break
        return [self.row(sid) for sid in hits]

    def bench(self) -> list[dict[str, Any]]:
        return [self.row(sid) for sid in self.bench_ids()]

    def summary(self) -> dict[str, Any]:
        """Per-lane means over the bench tracks both lanes scored, plus pooled label frames."""
        ids = self.bench_ids()
        docs = {sid: self.segments(sid) for sid in ids}
        out: dict[str, Any] = {
            "bench": len(ids),
            "reference": "rekordbox phrases (PSSI)",
            "with_phrases": sum(d["lanes"]["pssi"]["status"] == "ok" for d in docs.values()),
            "lanes": {},
        }
        for lane in SCORED_LANES:
            rows = [d["scores"][lane] for d in docs.values() if d["scores"][lane]["status"] == "ok"]
            agg: dict[str, Any] = {"scored": len(rows)}
            for k in SUMMARY_KEYS:
                vals = [r[k] for r in rows if r.get(k) is not None]
                agg[k] = round(statistics.fmean(vals), 3) if vals else None
            total = {k: sum(r["frames"][k] for r in rows) for k in ("frames", "agree", "drop_tp", "drop_fp", "drop_fn")}
            acc, f1 = agreement_from_counts(total)
            agg["label_acc_pooled"] = round(acc, 3) if acc is not None else None
            agg["drop_f1_pooled"] = round(f1, 3) if f1 is not None else None
            agg["label_frames"] = total["frames"]
            out["lanes"][lane] = agg
        both = [d for d in docs.values() if all(d["scores"][k]["status"] == "ok" for k in SCORED_LANES)]
        out["both_scored"] = len(both)
        # The head-to-head: each lane's means over the SAME tracks, the only fair comparison.
        out["paired"] = {
            lane: {k: _mean([d["scores"][lane][k] for d in both]) for k in PAIRED_KEYS} for lane in SCORED_LANES
        }
        return out


def _mean(vals: list[float | None]) -> float | None:
    got = [v for v in vals if v is not None]
    return round(statistics.fmean(got), 3) if got else None


# ----- the track document --------------------------------------------------------------


def _scores(lanes: dict[str, list[dict[str, Any]] | None], downbeats: list[float], duration: float) -> dict[str, Any]:
    ref = lanes["pssi"]
    out = {}
    for key in SCORED_LANES:
        est = lanes[key]
        if ref is None:
            out[key] = {"status": "unavailable", "reason": "no rekordbox phrases for this track"}
        elif est is None:
            out[key] = {"status": "unavailable", "reason": f"no {key} sections for this track"}
        else:
            out[key] = against_rekordbox(ref, est, downbeats, duration)
    return out


def library_segments(lib: Library, stable_id: str) -> dict[str, Any]:
    t = lib.track(stable_id)
    peaks = lib.peaks(stable_id)
    duration = peaks.shape[0] / DETAIL_COLUMNS_PER_S
    try:
        rb = read_rekordbox(lib.anlz_dat(stable_id), t.folder_path)
        rb_reason = None
    except RekordboxUnavailable as exc:
        rb, rb_reason = None, str(exc)
    beats = rb["beats"] if rb else []
    downbeats = [float(b["t"]) for b in beats if int(b["n"]) == 1]
    sections: dict[str, list[dict[str, Any]] | None] = {"pssi": None, "own_fast": None, "allin1": None}

    if rb is None:
        pssi = _lane_off("unavailable", f"no rekordbox analysis: {rb_reason}")
    elif not rb["phrases"]:
        pssi = _lane_off("unavailable", "rekordbox has not phrase-analyzed this track (no PSSI tag)")
    else:
        ref = sections["pssi"] = pssi_sections(rb["phrases"], rb["mood"])
        pssi = _lane_ok(
            ref,
            [],
            downbeats,
            mood=MOOD_NAMES.get(rb["mood"] or 0, str(rb["mood"])),
            labels="mapped",
            labels_reason="Up/Down/Intro/Chorus/Outro map to DJ labels; Verse/Bridge do not",
        )
        for span in pssi["sections"]:  # the lane shows rekordbox's own name; dj_label carries the mapping
            span["label"] = span["model_label"]

    if not downbeats:
        own = _lane_off("unavailable", "no grid: rekordbox has no beatgrid (PQTZ) for this track")
        allin1 = _lane_off("unavailable", "no grid to quantize All-In-One onto")
    else:
        t0 = time.perf_counter()
        est = segment(peaks, downbeats, duration)
        ms = round((time.perf_counter() - t0) * 1000.0, 1)
        if est["status"] != "ok":
            own = _lane_off("unavailable", f"own-fast failed: {est['reason']}")
        else:
            sections["own_fast"] = est["sections"]
            own = _lane_ok(
                est["sections"],
                est["moments"],
                downbeats,
                producer_version=OWN_FAST_VERSION,
                labels=est["dj_labels"]["status"],
                labels_reason=est["dj_labels"].get("reason"),
                segment_ms=ms,
                phrase_offset=est["phrase_offset"],
            )
        raw = lib.allin1_raw(stable_id)
        if raw is None:
            allin1 = _lane_off("pending", "All-In-One has not been run on this track (bench set only)")
        elif raw.get("status") != "ok":
            allin1 = _lane_off("unavailable", f"All-In-One runner failed: {raw.get('reason')}")
        else:
            doc = build_sidecar(stable_id, raw, downbeats, GRID_ID)
            if doc["status"] != "ok":
                allin1 = _lane_off("unavailable", f"All-In-One sidecar failed: {doc['reason']}")
            else:
                sections["allin1"] = doc["sections"]
                allin1 = _lane_ok(
                    doc["sections"],
                    doc.get("moments") or [],
                    downbeats,
                    producer_version=ALLIN1_VERSION,
                    labels=doc["dj_labels"]["status"],
                    labels_reason=doc["dj_labels"].get("reason"),
                    runner_seconds=raw.get("seconds"),
                )
    row = lib.row(stable_id)
    return {
        "mode": "library",
        "stable_id": stable_id,
        "title": row["title"],
        "genre": t.genre,
        "bpm": t.bpm,
        "duration_s": round(duration, 3),
        "grid": {
            "source": GRID_SOURCE if downbeats else None,
            "downbeats": downbeats,
            "beats": [float(b["t"]) for b in beats],
            "lead_in_s": rb["lead_in_s"] if rb else None,
        },
        "peaks": {
            "url": f"/api/library/tracks/{stable_id}/peaks",
            "columns": int(peaks.shape[0]),
            "columns_per_s": DETAIL_COLUMNS_PER_S,
            "bands": ["low", "mid", "high"],
        },
        "audio": {"url": f"/api/library/tracks/{stable_id}/audio"},
        "lanes": {"own_fast": own, "allin1": allin1, "pssi": pssi},
        "reference": "pssi",
        "scores": _scores(sections, downbeats, duration),
    }


# ----- bench selection --------------------------------------------------------------------


def _bpm_band(bpm: float | None) -> str:
    if bpm is None:
        return "unknown"
    for hi, name in ((100, "<100"), (118, "100-117"), (124, "118-123"), (128, "124-127"), (135, "128-134")):
        if bpm < hi:
            return name
    return "135+"


def _genre_family(genre: str | None) -> str:
    g = (genre or "").lower()
    for key, fam in (
        ("techno", "techno"),
        ("house", "house"),
        ("trance", "trance"),
        ("drum", "dnb"),
        ("hip", "hiphop"),
        ("rap", "hiphop"),
        ("disco", "disco"),
        ("pop", "pop"),
        ("rock", "rock"),
        ("electro", "electro"),
        ("dance", "dance"),
    ):
        if key in g:
            return fam
    return "other" if g else "untagged"


def _eligible(lib: Library) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """(stable_id, stratum) of every scorable track, and the denominator at each filter on the way."""
    counts = {
        "library_mapped": len(lib.tracks),
        "with_anlz_path": 0,
        "anlz_on_disk": 0,
        "audio_on_disk": 0,
        "anlz_unreadable": 0,
        "with_phrases": 0,
    }
    eligible: list[tuple[str, str]] = []
    for sid, t in lib.tracks.items():
        if not t.anlz_path:
            continue
        counts["with_anlz_path"] += 1
        try:
            dat = lib.anlz_dat(sid)
        except RekordboxUnavailable:
            continue
        counts["anlz_on_disk"] += 1
        try:
            lib.audio_path(sid)
        except FileNotFoundError:
            continue
        counts["audio_on_disk"] += 1
        try:
            rb = read_rekordbox(dat, t.folder_path)
        except Exception as exc:
            counts["anlz_unreadable"] += 1  # counted out, loudly, never silently
            print(f"[WARN] {sid}: ANLZ unreadable ({type(exc).__name__}: {exc})", flush=True)
            continue
        if not rb["phrases"] or sum(int(b["n"]) == 1 for b in rb["beats"]) < 2 * CONTROL_BARS:
            continue
        counts["with_phrases"] += 1
        eligible.append((sid, f"{_genre_family(t.genre)}|{_bpm_band(t.bpm)}"))
    return eligible, counts


def select_bench(lib: Library, size: int, seed: int) -> dict[str, Any]:
    """Stratified bench: tracks with PSSI phrases AND audio here, spread over genre family x BPM band.

    Denominators are counted on the way and returned, so the selection says
    what it was drawn from. Round-robin over strata (largest first), seeded.
    """
    eligible, counts = _eligible(lib)
    strata: dict[str, list[str]] = {}
    for sid, key in eligible:
        strata.setdefault(key, []).append(sid)
    rng = random.Random(seed)
    for ids in strata.values():
        rng.shuffle(ids)
    order = sorted(strata, key=lambda k: (-len(strata[k]), k))
    picked: list[str] = []
    while len(picked) < size and any(strata[k] for k in order):
        for k in order:
            if strata[k] and len(picked) < size:
                picked.append(strata[k].pop())
    by_stratum: dict[str, int] = {}
    for sid in picked:
        key = next(k for s, k in eligible if s == sid)
        by_stratum[key] = by_stratum.get(key, 0) + 1
    return {
        "stable_ids": picked,
        "seed": seed,
        "size": size,
        "denominators": counts,
        "strata_available": {k: len(v) + by_stratum.get(k, 0) for k, v in strata.items()},
        "strata_picked": by_stratum,
        "method": "eligible = rekordbox ANLZ on disk + audio on disk + PSSI phrases + at least 32 bars; "
        "strata = genre family x BPM band; round-robin over strata, largest first, seeded shuffle",
    }


__all__ = [
    "Library",
    "LibraryTrack",
    "RekordboxUnavailable",
    "UnknownTrack",
    "against_rekordbox",
    "pssi_sections",
    "read_rekordbox",
    "select_bench",
]
