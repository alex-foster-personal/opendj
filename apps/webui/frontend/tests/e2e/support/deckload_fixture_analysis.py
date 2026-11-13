from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from apps.analysis.canonical import canonical_pointer
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import fetch_records

from apps.webui.frontend.tests.e2e.support.deckload_fixture_core import _uv
from apps.webui.frontend.tests.e2e.support.deckload_fixture_model import (
    FixtureTrack,
    REPOSITORY_ROOT,
)

def _stable_ids_in_track_order(
    rows: list[tuple[str, str | None, str | None]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> list[str]:
    """Line ingested rows up with ``tracks`` by filename, in tuple order."""
    by_filename: dict[str, str] = {}
    for stable_id, _title, file_path in rows:
        if file_path is None:
            continue
        by_filename[Path(file_path).name] = stable_id
    ordered: list[str] = []
    missing: list[str] = []
    for track in tracks:
        stable_id = by_filename.get(track.filename)
        if stable_id is None:
            missing.append(track.filename)
            continue
        ordered.append(stable_id)
    if missing or len(ordered) != len(tracks):
        raise SystemExit(
            f"[ERROR] {label} could not match every generated file to an ingested row "
            f"(missing {missing!r})"
        )
    return ordered


def _pairs_for_generated_tracks(
    rows: list[tuple[str, str | None, str | None]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> list[list[str]]:
    """Map ingested rows onto generated audio files, in a pairs-json shape."""
    by_filename = {track.filename: track for track in tracks}
    pairs: list[list[str]] = []
    for stable_id, _title, file_path in rows:
        if not file_path:
            raise SystemExit(f"[ERROR] {label} row {stable_id!r} has no file_path")
        audio_path = Path(file_path)
        if audio_path.name not in by_filename:
            continue
        pairs.append([stable_id, str(audio_path)])
    if len(pairs) != len(tracks):
        raise SystemExit(
            f"[ERROR] {label} could not map every generated track to an ingested row "
            f"(expected {len(tracks)}, got {len(pairs)})"
        )
    return pairs


def _assert_measured_downbeats(
    stored: list[dict[str, Any]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> None:
    """Fail closed when a fixture row is missing a measured bar phase."""
    if len(stored) != len(tracks):
        raise SystemExit(
            f"[ERROR] {label} wrote {len(stored)} librosa row(s) for {len(tracks)} track(s)"
        )
    for row in stored:
        record = AnalysisRecord.from_json(row["record_json"])
        if not record.downbeats_s:
            raise SystemExit(
                f"[ERROR] {label} track {record.stable_id} has no measured downbeats"
            )
        if record.downbeats_s != sorted(record.downbeats_s):
            raise SystemExit(
                f"[ERROR] {label} track {record.stable_id} downbeats are not increasing"
            )
        if record.duration_s and record.downbeats_s[-1] > record.duration_s:
            raise SystemExit(
                f"[ERROR] {label} track {record.stable_id} downbeats exceed duration"
            )
        if record.features_blob.get("downbeat_tracking") is not True:
            raise SystemExit(
                f"[ERROR] {label} track {record.stable_id} did not establish bar phase"
            )


def _sid_for_filename(
    rows: list[tuple[str, str | None, str | None]], filename: str
) -> str:
    return next(
        stable_id
        for stable_id, _title, file_path in rows
        if file_path is not None and Path(file_path).name == filename
    )


def _assert_fixture_bpms(
    rows: list[tuple[str, str | None, str | None]],
    stored: list[dict[str, Any]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> None:
    """Every supplied track must measure near its generated tempo."""
    by_stable_id = {
        row["stable_id"]: AnalysisRecord.from_json(row["record_json"]) for row in stored
    }
    for track in tracks:
        try:
            stable_id = _sid_for_filename(rows, track.filename)
        except StopIteration:
            raise SystemExit(
                f"[ERROR] {label} track {track.filename} has no ingested row"
            ) from None
        record = by_stable_id.get(stable_id)
        if record is None:
            raise SystemExit(
                f"[ERROR] {label} track {track.filename} has no librosa analysis record"
            )
        measured_bpm = record.bpm
        if abs(measured_bpm - track.bpm) > 4.0:
            raise SystemExit(
                f"[ERROR] {label} track {track.filename} measured {measured_bpm:.1f} bpm, "
                f"expected near {track.bpm}"
            )


def _run_librosa_analysis(
    data_dir: Path,
    rows: list[tuple[str, str | None, str | None]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> None:
    """Run the production librosa analyzer and fail closed on missing downbeats."""
    pairs = _pairs_for_generated_tracks(rows, tracks, label)
    pairs_path = data_dir / "analysis-pairs.json"
    pairs_path.write_text(json.dumps(pairs, indent=2) + "\n", encoding="utf-8")
    env = dict(os.environ)
    env["MDT_DATA_DIR"] = str(data_dir)
    env.pop("WEB_CONCURRENCY", None)
    command = [
        _uv(),
        "run",
        "--no-sync",
        "--extra",
        "analysis",
        "python",
        "-m",
        "apps.analysis.run",
        "--pairs-json",
        str(pairs_path),
        "--backend",
        "librosa",
    ]
    result = subprocess.run(
        command, cwd=REPOSITORY_ROOT, env=env, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"[ERROR] {label} librosa analysis failed with exit code {result.returncode}"
        )
    pairs_path.unlink(missing_ok=True)

    state_db_path = data_dir / "state" / "state.db"
    analysis_filenames = {track.filename for track in tracks}
    stable_ids = [
        stable_id
        for stable_id, _title, file_path in rows
        if file_path and Path(file_path).name in analysis_filenames
    ]
    stored = fetch_records(stable_ids=stable_ids, backend="librosa", db_path=state_db_path)
    _assert_measured_downbeats(stored, tracks, label)
    _assert_fixture_bpms(rows, stored, tracks, label)


#: The own beatgrid producer's backend name (``apps.analysis.backends.own_beatgrid``).
OWN_BEATGRID_BACKEND: str = "own_beatgrid.backfill"
#: Where the producer finds its verified Beat This! checkpoint
#: (``apps.analysis_beatgrid.weights.WEIGHTS_PATH_ENV``).
OWN_BEATGRID_WEIGHTS_ENV: str = "MDT_BEATGRID_WEIGHTS"
#: A grid this far from the generated tempo is a wrong grid, not jitter.
OWN_BEATGRID_BPM_TOLERANCE: float = 0.5


def _run_own_beatgrid_analysis(
    data_dir: Path,
    rows: list[tuple[str, str | None, str | None]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> None:
    """Run the PRODUCTION own beatgrid producer and fail closed on any non-ok lane.

    Since #3561 the deck refuses the legacy librosa ``/beatgrid-fallback`` grid
    for a track whose own lane reports ``missing``, which is every unmapped
    fixture track, so without this step no fixture deck has a grid and Beat
    Sync cannot be exercised at all. This is the same backfill a real library
    gets (``apps.analysis.run --backend own_beatgrid.backfill``: Beat This! on
    the generated audio, grid fit, canonical pointer), so ``/anlz`` then serves
    a measured ``source: own, status: ok`` grid and the #3561 rule is untouched.
    Nothing is written by hand.
    """
    weights = os.environ.get(OWN_BEATGRID_WEIGHTS_ENV, "").strip()
    if not weights or not Path(weights).is_file():
        raise SystemExit(
            f"[ERROR] {label} --seed-own-beatgrid needs {OWN_BEATGRID_WEIGHTS_ENV} naming "
            f"the verified Beat This! checkpoint file, got {weights!r}"
        )
    pairs = _pairs_for_generated_tracks(rows, tracks, label)
    pairs_path = data_dir / "own-beatgrid-pairs.json"
    pairs_path.write_text(json.dumps(pairs, indent=2) + "\n", encoding="utf-8")
    env = dict(os.environ)
    env["MDT_DATA_DIR"] = str(data_dir)
    env.pop("WEB_CONCURRENCY", None)
    command = [
        _uv(), "run", "--no-sync", "python", "-m", "apps.analysis.run",
        "--pairs-json", str(pairs_path), "--backend", OWN_BEATGRID_BACKEND,
    ]
    result = subprocess.run(
        command, cwd=REPOSITORY_ROOT, env=env, capture_output=True, text=True, check=False,
    )
    pairs_path.unlink(missing_ok=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"[ERROR] {label} own beatgrid analysis failed with exit code {result.returncode}"
        )
    _assert_own_beatgrids(data_dir, rows, tracks, label)


def _assert_own_beatgrids(
    data_dir: Path,
    rows: list[tuple[str, str | None, str | None]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> None:
    """Every generated track has a canonical own grid, ok, at its generated tempo."""
    by_filename = {track.filename: track for track in tracks}
    conn = sqlite3.connect(f"file:{data_dir / 'state' / 'state.db'}?mode=ro", uri=True)
    try:
        for stable_id, _title, file_path in rows:
            if file_path is None or Path(file_path).name not in by_filename:
                continue
            track = by_filename[Path(file_path).name]
            pointer = canonical_pointer(conn, stable_id, "beatgrid")
            if pointer is None:
                raise SystemExit(
                    f"[ERROR] {label} {track.filename} has no canonical own beatgrid"
                )
            row = conn.execute(
                "SELECT record_json FROM analysis "
                "WHERE stable_id = ? AND backend = ? AND backend_version = ?",
                (stable_id, pointer[0], pointer[1]),
            ).fetchone()
            lane = AnalysisRecord.from_json(row[0]).lanes.get("beatgrid")
            if lane is None or lane.status != "ok":
                status = None if lane is None else (lane.status, lane.reason)
                raise SystemExit(
                    f"[ERROR] {label} {track.filename} own beatgrid is not ok: {status}"
                )
            measured = float(lane.payload["bpm"])
            if abs(measured - track.bpm) > OWN_BEATGRID_BPM_TOLERANCE:
                raise SystemExit(
                    f"[ERROR] {label} {track.filename} own beatgrid measured "
                    f"{measured:.2f} bpm, generated at {track.bpm}"
                )
            if not any(int(beat["n"]) == 1 for beat in lane.payload["beats"]):
                raise SystemExit(
                    f"[ERROR] {label} {track.filename} own beatgrid has no beat-1 marker"
                )
    finally:
        conn.close()

