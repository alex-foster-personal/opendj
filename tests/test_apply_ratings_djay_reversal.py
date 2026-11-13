"""SYNC-12981: verified djay rating writes leave a replayable reversal record.

[if] a verified djay rating write runs [then] its prior value lands in a replayable reversal artifact, [else stop].
"""

from __future__ import annotations

import json
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.djay_db import extract_rating_from_tsaf
from apps.sync.apply_ratings import (
    _apply_djay_rating_targets,
    replay_djay_rating_reversal,
)
from apps.sync.safety import SafetyAbort

pytestmark = pytest.mark.requirement("SYNC-12981")

REPO_ROOT = Path(__file__).resolve().parents[1]


def _blob(rating: int) -> bytes:
    """Return a minimal TSAF user-data blob carrying ``rating``."""
    body = b"\x0f" + bytes([rating]) + b"\x08rating\x00" if rating else b""
    return b"TSAF" + b"\x00" * 12 + body + b"\x0b\x05\x00\x00"


def _make_djay_db(tmp_path: Path, ratings: dict[str, int]) -> Path:
    """Create a djay-shaped sqlite DB with one user-data row per uuid."""
    db_dir = tmp_path / "djay"
    db_dir.mkdir()
    db = db_dir / "MediaLibrary.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE database2 (collection TEXT, key TEXT, data BLOB)")
        for uuid, rating in ratings.items():
            con.execute(
                "INSERT INTO database2 VALUES ('mediaItemUserData', ?, ?)",
                (uuid, _blob(rating)),
            )
    return db


def _rating(db: Path, uuid: str) -> int:
    """Read back the djay rating for ``uuid``."""
    with sqlite3.connect(db) as con:
        row = con.execute("SELECT data FROM database2 WHERE key = ?", (uuid,)).fetchone()
    return extract_rating_from_tsaf(row[0])


@pytest.fixture
def _open_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the process and iCloud rails so the session runs on tmp files."""
    monkeypatch.setattr("apps.sync.safety._is_running", lambda _name: False)
    monkeypatch.setattr("apps.sync.safety.assert_icloud_quiesced", lambda *_a, **_kw: None)


def _reversal_dir(tmp_path: Path) -> Path:
    """Return the single reversal directory the session created."""
    dirs = list((tmp_path / "sync" / "reversal").iterdir())
    assert len(dirs) == 1
    return dirs[0]


@pytest.mark.usefixtures("_open_gates")
def test_verified_djay_write_records_prior_rating(tmp_path: Path) -> None:
    """[if] a djay rating target is changed and verifies [then] reverse.jsonl and reverse.sh record its prior value, [else stop]."""
    db = _make_djay_db(tmp_path, {"u-1": 3, "u-2": 0})
    written = _apply_djay_rating_targets([("u-1", 5), ("u-2", 4)], djay_db_path=db, flag_ok=True)
    assert written == 2
    rdir = _reversal_dir(tmp_path)
    records = [json.loads(line) for line in (rdir / "reverse.jsonl").read_text().splitlines()]
    assert [(r["track_id"], r["prior"], r["new"]) for r in records] == [
        ("u-1", 3, 5),
        ("u-2", 0, 4),
    ]
    assert all(r["target"] == "djay" and r["field"] == "rating" for r in records)
    script = (rdir / "reverse.sh").read_text()
    restore_lines = [ln for ln in script.splitlines() if "restore_djay_rating" in ln]
    assert len(restore_lines) == 2


@pytest.mark.usefixtures("_open_gates")
def test_unverified_djay_write_records_no_reversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a djay rating target does not verify [then] the batch aborts with no reverse operation logged, [else stop]."""
    db = _make_djay_db(tmp_path, {"u-1": 3})
    monkeypatch.setattr("apps.sync.apply_ratings._verify_djay_rating", lambda *_a: False)
    with pytest.raises(SafetyAbort, match="verify_readback failed"):
        _apply_djay_rating_targets([("u-1", 5)], djay_db_path=db, flag_ok=True)
    rdir = _reversal_dir(tmp_path)
    assert not (rdir / "reverse.jsonl").exists()
    assert "restore_djay_rating" not in (rdir / "reverse.sh").read_text()


@pytest.mark.usefixtures("_open_gates")
def test_replay_restores_prior_ratings_on_copied_db(tmp_path: Path) -> None:
    """[if] a reversal record is replayed against the copied database [then] every prior rating returns, [else stop]."""
    db = _make_djay_db(tmp_path, {"u-1": 3, "u-2": 0})
    _apply_djay_rating_targets([("u-1", 5), ("u-2", 4)], djay_db_path=db, flag_ok=True)
    copy = tmp_path / "copy.db"
    shutil.copy2(db, copy)
    assert (_rating(copy, "u-1"), _rating(copy, "u-2")) == (5, 4)
    records = _reversal_dir(tmp_path) / "reverse.jsonl"
    assert replay_djay_rating_reversal(records, copy) == 2
    assert (_rating(copy, "u-1"), _rating(copy, "u-2")) == (3, 0)


@pytest.mark.usefixtures("_open_gates")
def test_reverse_sh_line_is_executable(tmp_path: Path) -> None:
    """[if] the emitted reverse.sh restore line is run [then] the prior rating returns, [else stop]."""
    db = _make_djay_db(tmp_path, {"u-1": 2})
    _apply_djay_rating_targets([("u-1", 5)], djay_db_path=db, flag_ok=True)
    script = (_reversal_dir(tmp_path) / "reverse.sh").read_text()
    line = next(ln for ln in script.splitlines() if "restore_djay_rating" in ln)
    tokens = shlex.split(line)
    assert tokens[:3] == ["uv", "run", "python"]
    subprocess.run([sys.executable, *tokens[3:]], cwd=REPO_ROOT, check=True)
    assert _rating(db, "u-1") == 2
