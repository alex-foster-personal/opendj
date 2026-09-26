"""Throwaway library with one demucs vocal-cache entry for PreviewStrip overlay e2e.

Builds one ingested track with empty-PVDI ANLZ, runs ``python -m apps.vocals
one --force`` when ``MDT_LIVE_DEMUCS_ACCEPTANCE=1``, and emits a manifest for
Playwright. Never touches the canonical vocal-cache tree.

The live build needs REAL vocal audio: htdemucs finds zero vocal regions in the
synthetic tone-and-pulse wav the other e2e fixtures share (measured Sat 26 Sep
2026: coverage 0.0%, 0 regions), so the overlay could never paint. Pass
``--source-audio`` (a read-only file with vocals) and ``--clip-start-s``; the
builder clips ``CLIP_SECONDS`` of it to a wav inside the disposable data dir and
refuses to finish if demucs still finds no vocal region.

Usage::

    uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.vocals_demucs_fixture \\
        --data-dir /abs/path/to/vocals-demucs-data [--manifest /abs/manifest.json] \\
        --source-audio /abs/vocal-track.mp3 --clip-start-s 55
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.vocals import cache as vcache
from apps.webui.frontend.tests.e2e.support.deckload_fixture import (
    FIXTURE_REVISION,
    REPOSITORY_ROOT,
    _ingest_and_verify,
    _track_rows,
)

FIXTURE_REVISION_VOCALS: str = f"{FIXTURE_REVISION}-vocals-demucs-overlay-v6"
PLAYLIST_ID = "vocals-demucs-overlay"
TRACK_FILENAME = "vocals-demucs-overlay-track.wav"
#: Clip length taken from --source-audio: long enough to hold a vocal phrase,
#: short enough that the CPU demucs pass fits inside the webServer boot budget.
CLIP_SECONDS: float = 40.0
_PVDI_HEAD = b"PMAI" + struct.pack(">II", 28, 28)
_PWAV_COLUMNS = 400
_PWAV_MAX_HEIGHT = 31


@dataclass(frozen=True)
class VocalsDemucsFixture:
    stable_id: str
    playlist_id: str
    revision: str
    demucs_ready: bool


def _empty_pmai() -> bytes:
    """A valid ANLZ PMAI container with no sections (no PWAV, no PVDI)."""
    return _PVDI_HEAD + b"\x00" * (28 - len(_PVDI_HEAD))


def _pwav_heights_from_wav(wav_path: Path) -> bytes:
    """Peak-per-column PWAV heights (0..31) measured from the clipped wav.

    PreviewStrip renders nothing without an ANLZ waveform, so the .DAT carries
    a PWAV preview computed from the same real audio demucs analyzes.
    """
    with wave.open(str(wav_path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise SystemExit(f"[ERROR] expected 16-bit pcm in {wav_path}")
        frames = wav.readframes(wav.getnframes())
        channels = wav.getnchannels()
    samples = np.frombuffer(frames, dtype="<i2").reshape(-1, channels)
    mono = np.abs(samples.astype(np.int32)).max(axis=1)
    buckets = np.array_split(mono, _PWAV_COLUMNS)
    peaks = np.array([int(b.max()) if b.size else 0 for b in buckets], dtype=np.int64)
    if int(peaks.max()) <= 0:
        raise SystemExit(f"[ERROR] {wav_path} is silent; no PWAV preview to write")
    heights = (peaks * _PWAV_MAX_HEIGHT) // int(peaks.max())
    return heights.astype(np.uint8).tobytes()


def _pmai_with_pwav(heights: bytes) -> bytes:
    """PMAI container holding one PWAV section (the .DAT preview waveform)."""
    pwav_head = 20
    section = (
        b"PWAV"
        + struct.pack(">III", pwav_head, pwav_head + len(heights), len(heights))
        + struct.pack(">I", 0x00010000)
        + heights
    )
    return _PVDI_HEAD[:4] + struct.pack(">II", 28, 28 + len(section)) + b"\x00" * 16 + section


def _write_anlz_siblings(audio_path: Path) -> tuple[Path, Path]:
    dat = audio_path.with_suffix(".DAT")
    twoex = audio_path.with_suffix(".2EX")
    dat.write_bytes(_pmai_with_pwav(_pwav_heights_from_wav(audio_path)))
    twoex.write_bytes(_empty_pmai())
    return dat, twoex


def _write_vendor_rows(
    data_dir: Path, stable_id: str, title: str, audio_path: Path, dat_path: Path
) -> None:
    master = sqlite3.connect(data_dir / "master.plain.db")
    # Columns the hydrated listing reads (rb_vendor_pkg.track_rows.bulk_rb_meta).
    master.execute(
        "CREATE TABLE IF NOT EXISTS djmdContent ("
        "ID TEXT, Title TEXT, Length INTEGER, FolderPath TEXT, ImagePath TEXT, "
        "AnalysisDataPath TEXT, Commnt TEXT, GenreID TEXT, DJPlayCount INTEGER, "
        "rb_local_deleted INTEGER)"
    )
    master.execute(
        "CREATE TABLE IF NOT EXISTS djmdGenre "
        "(ID TEXT, Name TEXT, rb_local_deleted INTEGER)"
    )
    vendor_id = "vocals-demucs-overlay-v1"
    master.execute("DELETE FROM djmdContent WHERE ID = ?", (vendor_id,))
    master.execute(
        "INSERT INTO djmdContent (ID, Title, Length, FolderPath, AnalysisDataPath, "
        "DJPlayCount, rb_local_deleted) VALUES (?, ?, ?, ?, ?, 0, 0)",
        (vendor_id, title, int(CLIP_SECONDS), str(audio_path), str(dat_path)),
    )
    master.commit()
    master.close()

    # The ingested state.db carries the real track_vendor_ids schema, so go
    # through the production writer rather than a hand-rolled INSERT.
    conn = state_db.open_rw(data_dir / "state" / "state.db")
    writer = StateWriter(conn, actor="e2e-vocals-demucs-fixture")
    try:
        writer.set_vendor_id(stable_id, "rekordbox", vendor_id)
    finally:
        writer.close()
        conn.close()


def _stamp_duration_ms(state_db_path: Path, stable_id: str, audio_path: Path) -> None:
    """Record the clip's measured duration on the track row.

    PreviewStrip positions vocal bars by ``duration_ms`` and draws none when it
    is null. Folder ingest reads duration through the optional mutagen extra,
    which the dev env does not install, so measure it with ffprobe and write it
    back through the production writer.
    """
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    duration_ms = round(float(probe.stdout.strip()) * 1000)
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="e2e-vocals-demucs-fixture")
    try:
        row = conn.execute(
            "SELECT stable_id_tier, title, artists_json, album, isrc, file_path, "
            "content_hash, audio_hash FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"[ERROR] ingested track {stable_id} missing from {state_db_path}")
        tier, title, artists_json, album, isrc, file_path, content_hash, audio_hash = row
        writer.upsert_track(
            stable_id=stable_id,
            stable_id_tier=tier,
            title=title,
            artists=json.loads(artists_json or "[]"),
            album=album,
            isrc=isrc,
            duration_ms=duration_ms,
            file_path=file_path,
            content_hash=content_hash,
            audio_hash=audio_hash,
        )
    finally:
        writer.close()
        conn.close()


def _seed_playlist(state_db_path: Path, stable_id: str) -> None:
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="e2e-vocals-demucs-fixture")
    try:
        writer.insert_playlist(
            playlist_id=PLAYLIST_ID,
            name="Vocals demucs overlay",
            vendor="fixture",
            vendor_pl_id=PLAYLIST_ID,
        )
        writer.set_playlist_memberships(PLAYLIST_ID, [stable_id])
    finally:
        writer.close()
        conn.close()


def _clip_source_audio(source: Path, dest: Path, start_s: float) -> Path:
    """Clip ``CLIP_SECONDS`` of real audio into a wav the builder owns."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-ss", f"{start_s}", "-t", f"{CLIP_SECONDS}",
            "-i", str(source),
            "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le",
            str(dest),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not dest.is_file():
        raise SystemExit(
            f"[ERROR] ffmpeg could not clip {source} at {start_s}s: {result.stderr}"
        )
    return dest


def _run_one_live(data_dir: Path, stable_id: str) -> None:
    env = dict(os.environ)
    env["MDT_DATA_DIR"] = str(data_dir)
    env.setdefault("MDT_VOCAL_WORKER_DEVICE", "cpu")
    result = subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "apps.vocals",
            "one",
            "--force",
            "--stable-id",
            stable_id,
            "--data-dir",
            str(data_dir),
        ],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"[ERROR] vocals one --force failed with exit code {result.returncode}"
        )


def build(data_dir: Path, source_audio: Path, clip_start_s: float) -> VocalsDemucsFixture:
    data_dir = data_dir.resolve()
    demucs_ready = os.environ.get("MDT_LIVE_DEMUCS_ACCEPTANCE") == "1"
    if not source_audio.is_file():
        raise SystemExit(f"[ERROR] --source-audio is not a file: {source_audio}")
    marker = data_dir / ".vocals-demucs-fixture-revision"
    marker_text = f"{FIXTURE_REVISION_VOCALS}|{source_audio.resolve()}|{clip_start_s}"
    state_db = data_dir / "state" / "state.db"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == marker_text:
        rows = _track_rows(state_db)
        if len(rows) == 1:
            stable_id = rows[0][0]
            cache = vcache.cache_path(data_dir, stable_id)
            if cache.is_file():
                return VocalsDemucsFixture(
                    stable_id=stable_id,
                    playlist_id=PLAYLIST_ID,
                    revision=FIXTURE_REVISION_VOCALS,
                    demucs_ready=True,
                )
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    audio_dir = data_dir / "audio"
    files = [_clip_source_audio(source_audio, audio_dir / TRACK_FILENAME, clip_start_s)]
    rows = _ingest_and_verify(data_dir, audio_dir, files, "vocals-demucs-overlay")
    stable_id = rows[0][0]
    audio_path = Path(rows[0][2] or "")
    dat_path, _twoex = _write_anlz_siblings(audio_path)
    _write_vendor_rows(data_dir, stable_id, TRACK_FILENAME, audio_path, dat_path)
    _stamp_duration_ms(state_db, stable_id, audio_path)
    _seed_playlist(state_db, stable_id)
    if demucs_ready:
        _run_one_live(data_dir, stable_id)
        cache_file = vcache.cache_path(data_dir, stable_id)
        if not cache_file.is_file():
            raise SystemExit("[ERROR] demucs cache missing after one --force")
        regions = json.loads(cache_file.read_text(encoding="utf-8"))["regions"]
        if not regions:
            raise SystemExit(
                f"[ERROR] demucs found zero vocal regions in {source_audio} "
                f"from {clip_start_s}s; the overlay would have nothing to paint"
            )
    marker.write_text(f"{marker_text}\n", encoding="utf-8")
    return VocalsDemucsFixture(
        stable_id=stable_id,
        playlist_id=PLAYLIST_ID,
        revision=FIXTURE_REVISION_VOCALS,
        demucs_ready=demucs_ready,
    )


def write_manifest(path: Path, fixture: VocalsDemucsFixture) -> None:
    payload = {
        "revision": fixture.revision,
        "stable_id": fixture.stable_id,
        "playlist_id": fixture.playlist_id,
        "demucs_ready": fixture.demucs_ready,
        "tracks": [{"stable_id": fixture.stable_id, "title": TRACK_FILENAME}],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(payload, indent=2)}\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--source-audio", type=Path, default=None)
    parser.add_argument("--clip-start-s", type=float, default=None)
    args = parser.parse_args(argv)
    if not args.data_dir.is_absolute():
        print(f"[ERROR] --data-dir must be absolute, got {args.data_dir!r}", file=sys.stderr)
        return 2
    if args.manifest is not None and not args.manifest.is_absolute():
        print(f"[ERROR] --manifest must be absolute, got {args.manifest!r}", file=sys.stderr)
        return 2
    if args.source_audio is None or args.clip_start_s is None:
        print(
            "[ERROR] --source-audio and --clip-start-s are required "
            "(synthetic audio yields zero demucs vocal regions)",
            file=sys.stderr,
        )
        return 2
    if not args.source_audio.is_absolute():
        print(
            f"[ERROR] --source-audio must be absolute, got {args.source_audio!r}",
            file=sys.stderr,
        )
        return 2
    fixture = build(args.data_dir, args.source_audio, args.clip_start_s)
    print(
        f"[vocals-demucs-fixture] revision={fixture.revision} "
        f"stable_id={fixture.stable_id} demucs_ready={fixture.demucs_ready} "
        f"data_dir={args.data_dir}"
    )
    if args.manifest is not None:
        write_manifest(args.manifest, fixture)
        print(f"[vocals-demucs-fixture] manifest={args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
