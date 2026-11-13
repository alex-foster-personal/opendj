"""Shared fixtures and helpers for relink tests (split from test_relink.py)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from apps.reconcile import index_disk, match, relink
from apps.shared.state import db as state_db_mod
from apps.shared.state.writer import StateWriter

# ----- fixtures ---------------------------------------------------------


def _make_state_db(path: Path, rows: list[dict[str, object]]) -> Path:
    """Create a state DB holding ``rows`` via the real writer path."""
    conn = state_db_mod.open_rw(path)
    try:
        with StateWriter(conn, actor="test") as writer:
            for row in rows:
                writer.upsert_track(
                    stable_id=str(row["stable_id"]),
                    stable_id_tier=str(row.get("tier", "inferred")),
                    title=row.get("title"),  # type: ignore[arg-type]
                    artists=list(row.get("artists", [])),  # type: ignore[arg-type]
                    album=row.get("album"),  # type: ignore[arg-type]
                    isrc=row.get("isrc"),  # type: ignore[arg-type]
                    duration_ms=row.get("duration_ms"),  # type: ignore[arg-type]
                    file_path=row.get("file_path"),  # type: ignore[arg-type]
                    content_hash=row.get("content_hash"),  # type: ignore[arg-type]
                )
    finally:
        conn.close()
    return path


def _rows(db: Path) -> dict[str, tuple]:
    """Every ``tracks`` row keyed by stable_id, for before/after comparison."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = conn.execute(f"SELECT {', '.join(relink.TRACK_COLUMNS)} FROM tracks ORDER BY stable_id")
        return {r[0]: tuple(r) for r in cur.fetchall()}
    finally:
        conn.close()


def _entry(sid: str, old: str, new: str, conf: float = 0.95) -> relink.RelinkEntry:
    return relink.RelinkEntry(
        stable_id=sid,
        old_path=old,
        new_path=new,
        tier="basename-exact-unique",
        confidence=conf,
        timestamp="2026-07-28T00:00:00+00:00",
    )


def _audio(root: Path, name: str, payload: bytes = b"x" * 64) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    dst = root / name
    dst.write_bytes(payload)
    return dst


@pytest.fixture
def ambient_temp_neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real files under a classification-neutral ancestry; production untouched.

    ``tiebreak.classify_path`` substring-matches the candidate string as
    given, and "/tmp/" is a real STAGING_SEGMENTS entry. pytest's tmp_path is
    /tmp/pytest-of-*/... on Linux but /private/var/folders/... on macOS, so a
    "Manual Library" fixture spelled as an ABSOLUTE path classified curated
    on macOS and staging on Linux. That is correct production behaviour (a
    library sitting in the system temp dir IS a staging copy), but it means
    the ambient temp root, not the folder names the test spells out, decides
    the class.

    Neutral ancestry without altering any production global: chdir into
    tmp_path and hand the scorer candidate paths RELATIVE to it
    ("Manual Library/dup.mp3"). ``os.path.exists`` resolves them against the
    cwd, so the files are REAL on disk (``score_rows`` hard-rejects a missing
    candidate at apps/reconcile/tiebreak.py:296, which short-circuits
    ``_verdict`` before the RESOLVE_MARGIN rule these tests pin -- synthetic
    paths would make them pass vacuously), while the string ``classify_path``
    sees contains only the folder names the test chose. STAGING_SEGMENTS and
    CURATED_SEGMENTS ship exactly as production defines them; the "/tmp/"
    rule stays covered by the classify_path table below.
    """
    monkeypatch.chdir(tmp_path)


def _disk_entry(
    path: str,
    *,
    size: int = 1000,
    duration: float | None = None,
    isrc: str | None = None,
    title: str | None = None,
    artist: str | None = None,
    dataless: bool = False,
) -> index_disk.DiskAudio:
    return index_disk.DiskAudio(
        path=path,
        size_bytes=size,
        mtime=0.0,
        tags_read=True,
        duration_s=duration,
        title=title,
        artist=artist,
        isrc=isrc,
        dataless=dataless,
    )


def _result(
    sid: str,
    *,
    bucket: str = "relinkable-auto",
    path: str | None = "/gone/x.mp3",
    candidates: list[match.Candidate] | None = None,
    ambiguity: str | None = None,
) -> match.RowResult:
    return match.RowResult(
        stable_id=sid,
        file_path=path,
        bucket=bucket,  # type: ignore[arg-type]
        reason="test",
        tier=candidates[0].tier if candidates else None,  # type: ignore[index]
        ambiguity=ambiguity,  # type: ignore[arg-type]
        candidates=candidates or [],
    )


def _cand(path: str, conf: float = 0.95, tier: str = "basename-exact-unique"):
    return match.Candidate(path=path, tier=tier, confidence=conf, reason="test")  # type: ignore[arg-type]


def _cli_data_dir(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A tmp data dir plus a disk root holding one relocated file."""
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    disk = tmp_path / "roots" / "Music"
    target = _audio(disk, "relocated.mp3")
    _make_state_db(
        data_dir / "state" / "state.db",
        [{"stable_id": "a", "file_path": "/Users/old/Music/relocated.mp3"}],
    )
    return data_dir, disk, target


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _real_state_db() -> Path:
    env = os.environ.get("MDT_DATA_DIR")
    from apps.shared import paths as shared_paths

    base = Path(env) if env else shared_paths.DATA_DIR
    return base / "state" / "state.db"
