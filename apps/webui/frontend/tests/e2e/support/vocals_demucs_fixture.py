"""Throwaway library with one demucs vocal-cache entry for PreviewStrip overlay e2e.

Builds one ingested track with empty-PVDI ANLZ, runs ``python -m apps.vocals
one --live`` when ``MDT_LIVE_DEMUCS_ACCEPTANCE=1``, and emits a manifest for
Playwright. Never touches the canonical vocal-cache tree.

Usage::

    uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.vocals_demucs_fixture \\
        --data-dir /abs/path/to/vocals-demucs-data [--manifest /abs/manifest.json]
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
from dataclasses import dataclass
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.vocals import cache as vcache
from apps.webui.frontend.tests.e2e.support.deckload_fixture import (
    FIXTURE_REVISION,
    FixtureTrack,
    REPOSITORY_ROOT,
    _ingest_and_verify,
    _track_rows,
    ensure_audio,
)

FIXTURE_REVISION_VOCALS: str = f"{FIXTURE_REVISION}-vocals-demucs-overlay-v1"
PLAYLIST_ID = "vocals-demucs-overlay"
BENCH_TRACK = FixtureTrack(
    filename="vocals-demucs-overlay-track.wav",
    bpm=128.0,
    seconds=12.0,
)
_PVDI_HEAD = b"PMAI" + struct.pack(">II", 28, 28)


@dataclass(frozen=True)
class VocalsDemucsFixture:
    stable_id: str
    playlist_id: str
    revision: str
    demucs_ready: bool


def _empty_2ex() -> bytes:
    return _PVDI_HEAD + b"\x00" * (28 - len(_PVDI_HEAD))


def _write_anlz_siblings(audio_path: Path) -> tuple[Path, Path]:
    dat = audio_path.with_suffix(".DAT")
    twoex = audio_path.with_suffix(".2EX")
    dat.write_bytes(b"dat")
    twoex.write_bytes(_empty_2ex())
    return dat, twoex


def _write_vendor_rows(
    data_dir: Path, stable_id: str, title: str, audio_path: Path, dat_path: Path
) -> None:
    master = sqlite3.connect(data_dir / "master.plain.db")
    master.execute(
        "CREATE TABLE IF NOT EXISTS djmdContent ("
        "ID TEXT, Title TEXT, Length INTEGER, FolderPath TEXT, "
        "AnalysisDataPath TEXT, rb_local_deleted INTEGER)"
    )
    vendor_id = "vocals-demucs-overlay-v1"
    master.execute("DELETE FROM djmdContent WHERE ID = ?", (vendor_id,))
    master.execute(
        "INSERT INTO djmdContent VALUES (?, ?, ?, ?, ?, 0)",
        (vendor_id, title, int(BENCH_TRACK.seconds), str(audio_path), str(dat_path)),
    )
    master.commit()
    master.close()

    state = sqlite3.connect(data_dir / "state" / "state.db")
    state.execute(
        "CREATE TABLE IF NOT EXISTS track_vendor_ids "
        "(stable_id TEXT, vendor TEXT, vendor_id TEXT)"
    )
    state.execute(
        "DELETE FROM track_vendor_ids WHERE stable_id = ?", (stable_id,)
    )
    state.execute(
        "INSERT INTO track_vendor_ids VALUES (?, 'rekordbox', ?)",
        (stable_id, vendor_id),
    )
    state.commit()
    state.close()


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
            "--live",
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
            f"[ERROR] vocals one --live failed with exit code {result.returncode}"
        )


def build(data_dir: Path) -> VocalsDemucsFixture:
    data_dir = data_dir.resolve()
    marker = data_dir / ".vocals-demucs-fixture-revision"
    state_db = data_dir / "state" / "state.db"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == FIXTURE_REVISION_VOCALS:
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
    files = ensure_audio(audio_dir, (BENCH_TRACK,))
    rows = _ingest_and_verify(data_dir, audio_dir, files, "vocals-demucs-overlay")
    stable_id = rows[0][0]
    audio_path = Path(rows[0][2] or "")
    dat_path, _twoex = _write_anlz_siblings(audio_path)
    _write_vendor_rows(data_dir, stable_id, BENCH_TRACK.filename, audio_path, dat_path)
    _seed_playlist(state_db, stable_id)
    demucs_ready = os.environ.get("MDT_LIVE_DEMUCS_ACCEPTANCE") == "1"
    if demucs_ready:
        _run_one_live(data_dir, stable_id)
        if not vcache.cache_path(data_dir, stable_id).is_file():
            raise SystemExit("[ERROR] demucs cache missing after one --live")
    marker.write_text(f"{FIXTURE_REVISION_VOCALS}\n", encoding="utf-8")
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
        "tracks": [{"stable_id": fixture.stable_id, "title": BENCH_TRACK.filename}],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(payload, indent=2)}\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.data_dir.is_absolute():
        print(f"[ERROR] --data-dir must be absolute, got {args.data_dir!r}", file=sys.stderr)
        return 2
    if args.manifest is not None and not args.manifest.is_absolute():
        print(f"[ERROR] --manifest must be absolute, got {args.manifest!r}", file=sys.stderr)
        return 2
    fixture = build(args.data_dir)
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
