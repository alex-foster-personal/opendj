# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#     "tensorflow>=2.16,<2.19",
#     "tensorflow-hub>=0.16",
#     "numpy<2.1",
#     "setuptools<81",
# ]
# ///
"""Live-recording detector: scores crowd sound (AudioSet applause/cheering/crowd) with YAMNet.

Standalone PEP 723 script so TensorFlow never enters the repo venv (house rule: heavy ML
deps stay out). Results go to a resumable JSONL cache; nothing is written to state.db.

Requirements
  ✔︎ R1 score crowd-sound share on edge windows (first/last 20 s) and 3 middle 10 s windows
     [if] a file cannot be decoded [then ⛔️ row has status=decode_error, never a verdict]
     [if] YAMNet class map lacks a crowd class [then ⛔️ raise at startup]
  ✔︎ R2 verdict live | likely | studio from thresholds in CFG, with the scores that drove it
     [if] edge crowd share >= CFG.LIVE_EDGE [then ⛔️ verdict is live]
  ✔︎ R3 resumable: ok rows are skipped; missing/decode_error rows retry; an incomplete
     final fragment is reported on stderr and repaired; any other malformed record aborts
     without altering the cache file
  ✔︎ R4 --paths for ingest (explicit files) or --db for backfill (one path per distinct audio_hash,
     preferring a copy on disk; duplicates share one verdict, matching the deduped clean list)

Usage
  uv run scripts/live_detect.py --paths a.mp3 b.flac --out live.jsonl
  uv run scripts/live_detect.py --db <state.db> --out live.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


class CFG:
    MODEL_URL = "https://tfhub.dev/google/yamnet/1"
    SR = 16000
    EDGE_S = 20.0
    MID_S = 10.0
    CROWD_CLASSES = ("Applause", "Cheering", "Crowd", "Clapping", "Hubbub, speech noise, speech babble")
    FRAME_THRESHOLD = 0.15  # per-frame max crowd prob above this counts as a crowd frame
    LIVE_EDGE = 0.30  # share of edge frames with crowd -> live
    LIKELY_EDGE = 0.12
    LIKELY_MID = 0.10


@dataclass
class Result:
    path: str
    status: str  # ok | decode_error | missing
    verdict: str | None = None
    edge_share: float | None = None
    mid_share: float | None = None
    peak: float | None = None
    duration_s: float | None = None
    error: str | None = None


# ----------------------------------------------------------------------------- audio
def _resolve_bin(name: str, env_vars: tuple[str, ...]) -> str:
    for var in env_vars:
        if os.environ.get(var):
            path = os.environ[var]
            if not os.access(path, os.X_OK):
                raise RuntimeError(f"{var}={path} is not executable")
            return path
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} not found: set {' or '.join(env_vars)} or put it on PATH")
    return found


FFMPEG = _resolve_bin("ffmpeg", ("MDT_FFMPEG", "ODJ_FFMPEG_BIN"))
_sibling_probe = Path(FFMPEG).with_name("ffprobe")
FFPROBE = str(_sibling_probe) if os.access(_sibling_probe, os.X_OK) else _resolve_bin("ffprobe", ("MDT_FFPROBE",))


def _probe_duration(path: str) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return float(out)


def _decode_window(path: str, start: float, length: float) -> np.ndarray:
    raw = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-ss",
            f"{max(start, 0):.2f}",
            "-t",
            f"{length:.2f}",
            "-i",
            path,
            "-ac",
            "1",
            "-ar",
            str(CFG.SR),
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def _windows(duration: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    edge = [(0.0, CFG.EDGE_S), (duration - CFG.EDGE_S, CFG.EDGE_S)]
    mid = [(duration * f - CFG.MID_S / 2, CFG.MID_S) for f in (0.3, 0.5, 0.7)]
    return edge, mid


# ----------------------------------------------------------------------------- model
class Model:
    def __init__(self) -> None:
        import tensorflow_hub as hub

        self.net = hub.load(CFG.MODEL_URL)
        class_map = Path(self.net.class_map_path().numpy().decode()).read_text().splitlines()
        names = [r["display_name"] for r in csv.DictReader(class_map)]
        missing = [c for c in CFG.CROWD_CLASSES if c not in names]
        if missing:
            raise RuntimeError(f"YAMNet class map lacks {missing}")
        self.crowd_idx = [names.index(c) for c in CFG.CROWD_CLASSES]

    def crowd_frames(self, wav: np.ndarray) -> np.ndarray:
        if wav.size < CFG.SR:  # under 1 s: no frames worth scoring
            return np.zeros(0, dtype=np.float32)
        scores, _, _ = self.net(wav)
        return scores.numpy()[:, self.crowd_idx].max(axis=1)


# ----------------------------------------------------------------------------- scoring
def _share(frames: list[np.ndarray]) -> tuple[float, float]:
    allf = np.concatenate(frames) if frames else np.zeros(0)
    if allf.size == 0:
        return 0.0, 0.0
    return float((allf > CFG.FRAME_THRESHOLD).mean()), float(allf.max())


def _verdict(edge: float, mid: float) -> str:
    if edge >= CFG.LIVE_EDGE:
        return "live"
    if edge >= CFG.LIKELY_EDGE or mid >= CFG.LIKELY_MID:
        return "likely"
    return "studio"


def score_file(model: Model, path: str) -> Result:
    if not Path(path).is_file():
        return Result(path, "missing")
    try:
        duration = _probe_duration(path)
        edge_w, mid_w = _windows(duration)
        edge = [model.crowd_frames(_decode_window(path, s, n)) for s, n in edge_w]
        mid = [model.crowd_frames(_decode_window(path, s, n)) for s, n in mid_w]
    except (subprocess.CalledProcessError, ValueError) as exc:
        return Result(path, "decode_error", error=str(exc)[:200])
    if sum(f.size for f in edge + mid) == 0:
        return Result(path, "decode_error", error="no scoreable audio frames decoded")
    edge_share, edge_peak = _share(edge)
    mid_share, mid_peak = _share(mid)
    return Result(
        path,
        "ok",
        _verdict(edge_share, mid_share),
        round(edge_share, 3),
        round(mid_share, 3),
        round(max(edge_peak, mid_peak), 3),
        round(duration, 1),
    )


# ----------------------------------------------------------------------------- io
def _paths_from_db(db: str) -> list[str]:
    """One path per distinct audio (deduped by audio_hash), preferring a copy present on disk."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select coalesce(audio_hash, file_path), file_path from tracks "
        "where deleted_at is null and file_path is not null order by 1, 2"
    ).fetchall()
    chosen: dict[str, str] = {}
    for key, path in rows:
        if key not in chosen or (not Path(chosen[key]).is_file() and Path(path).is_file()):
            chosen[key] = path
    return list(chosen.values())


_KNOWN_CACHE_STATUSES = frozenset({"ok", "missing", "decode_error"})


def _validate_cache_row(row: object, cache_path: Path, line_num: int) -> None:
    if not isinstance(row, dict):
        raise TypeError(f"{cache_path}:{line_num}: cache row must be a JSON object")
    path = row.get("path")
    if not isinstance(path, str):
        raise TypeError(f"{cache_path}:{line_num}: cache row missing string path")
    status = row.get("status")
    if not isinstance(status, str):
        raise TypeError(f"{cache_path}:{line_num}: cache row missing string status")
    if status not in _KNOWN_CACHE_STATUSES:
        raise ValueError(f"{cache_path}:{line_num}: unknown status {status!r}")


def _compact_cache(out: Path) -> set[str]:
    """Keep only successfully scored rows, rewrite the cache, return their paths.

    Dropping missing/decode_error rows makes them retry on the next run. An incomplete
    unterminated final fragment (no trailing newline) is dropped with a stderr notice.
    Any other malformed record raises before the cache file is modified.
    """
    if not out.exists():
        return set()
    text = out.read_text(encoding="utf-8")
    ends_with_newline = text.endswith("\n")
    parts = text.split("\n")
    if ends_with_newline and parts and parts[-1] == "":
        parts = parts[:-1]

    kept_lines: dict[str, str] = {}
    for index, line in enumerate(parts):
        line_num = index + 1
        is_final = index == len(parts) - 1
        interrupted_tail = is_final and not ends_with_newline
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            if interrupted_tail:
                preview = line if len(line) <= 120 else f"{line[:117]}..."
                print(
                    f"{out}:{line_num}: discarding incomplete cache fragment (will rescore): {preview!r}",
                    file=sys.stderr,
                )
                continue
            raise ValueError(f"{out}:{line_num}: malformed cache JSON: {exc}") from exc
        _validate_cache_row(row, out, line_num)
        if row["status"] == "ok":
            kept_lines[row["path"]] = json.dumps(row, separators=(",", ":"))
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(
        "".join(f"{line}\n" for line in kept_lines.values()),
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(out)
    return set(kept_lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--paths", nargs="+")
    src.add_argument("--db")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    paths = args.paths if args.paths else _paths_from_db(args.db)
    done = _compact_cache(args.out)
    todo = [p for p in paths if p not in done]
    print(f"[live_detect] {len(paths)} paths, {len(todo)} to score -> {args.out}", flush=True)
    model = Model()
    t0 = time.time()
    with args.out.open("a", encoding="utf-8", newline="\n") as fh:
        for i, p in enumerate(todo, 1):
            r = score_file(model, p)
            fh.write(json.dumps(asdict(r)) + "\n")
            fh.flush()
            if args.paths or i % 100 == 0:
                print(
                    f"[live_detect] {i}/{len(todo)} {time.time() - t0:.0f}s {r.status} {r.verdict} "
                    f"edge={r.edge_share} mid={r.mid_share} {Path(p).name}",
                    flush=True,
                )
    sys.exit(0)


if __name__ == "__main__":
    main()
