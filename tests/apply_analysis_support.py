"""Shared helpers for apply_analysis SYNC-05 tests (issue #10945 split)."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _owned_rollout_directory(tmp_path: Path):
    """Relative production rollout stamps belong to one test, not all workers.

    A test module takes this fixture by importing its name. Never load this module
    through ``pytest_plugins``: that registers the fixture for the whole session and
    every later test then runs with a temporary directory as its working directory.
    """
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield
    finally:
        os.chdir(previous)


def _make_diff(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "analysis-diff.csv"
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "rb_content_id",
                "djay_uuid",
                "field",
                "rb_value",
                "djay_value",
                "resolution",
                "action_hint",
            ],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return path


class _FakeContent:
    def __init__(self, id_: str, bpm: int = 0, color_id: int = 0) -> None:
        self.ID = id_
        self.BPM = bpm
        self.ColorID = color_id


class _OneShot:
    def __init__(self, value: _FakeContent | None) -> None:
        self._v = value

    def one(self) -> _FakeContent:
        if self._v is None:
            raise LookupError("no row")
        return self._v


class _FakeRBDB:
    def __init__(self, rows: dict[str, _FakeContent]) -> None:
        self._rows = rows
        self.commits = 0

    def get_content(self, *, ID: str) -> _OneShot:
        return _OneShot(self._rows.get(str(ID)))

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def _write_state_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, deleted_at TEXT);
        CREATE TABLE track_vendor_ids (
            stable_id TEXT, vendor TEXT, vendor_id TEXT, deleted_at TEXT
        );
        CREATE TABLE analysis_source_default (
            lane TEXT PRIMARY KEY, source TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE analysis_projection (
            stable_id TEXT, field TEXT, value, status TEXT, reason TEXT,
            confidence REAL, backend TEXT, backend_version TEXT, updated_at TEXT,
            PRIMARY KEY (stable_id, field)
        );
        """
    )
    return conn


#: A real, local WAV: PQTZ write-back reads each track's MP3 lead-in from its
#: file (NAE-22), and a WAV has none, so grids go back unshifted.
_TRACK_AUDIO = Path(__file__).resolve().parent / "fixtures" / "phase7-dedup" / "src.wav"


def _write_rb_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    folder = str(_TRACK_AUDIO).replace("'", "''")
    conn.executescript(
        f"""
        CREATE TABLE djmdContent (
            ID TEXT PRIMARY KEY, BPM INTEGER, KeyID TEXT,
            AnalysisDataPath TEXT, FolderPath TEXT DEFAULT '{folder}',
            rb_local_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE djmdKey (ID TEXT PRIMARY KEY, ScaleName TEXT);
        INSERT INTO djmdKey(ID, ScaleName) VALUES ('k1', 'Am');
        """
    )
    return conn


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_own_grid(
    state_path: Path,
    stable_id: str,
    beats: list[dict[str, float | int]],
    *,
    tempo_changes: tuple[dict[str, object], ...] = (),
    bpm: float = 128.0,
) -> None:
    from datetime import UTC, datetime

    from apps.analysis.lanes import LaneResult
    from apps.analysis.record import AnalysisRecord

    payload = {
        "beats": beats,
        "bpm": bpm,
        "bpm_confidence": 1.0,
        "octave_reason": "test",
        "first_downbeat_s": float(beats[0]["t"]) if beats else 0.0,
        "static_grid_untrusted": False,
        "tempo_changes": list(tempo_changes),
    }
    record = AnalysisRecord(
        stable_id=stable_id,
        backend="own_beatgrid.backfill",
        backend_version="1.0.0",
        analyzed_at=datetime.now(UTC),
        duration_s=float(beats[-1]["t"]) + 1.0 if beats else 60.0,
        sample_rate=44100,
        bpm=bpm,
        bpm_confidence=1.0,
        key_camelot="8A",
        key_openkey="",
        key_confidence=1.0,
        energy=5,
        producer="backfill",
        producer_version="1.0.0",
        uses_model=True,
        model_sha256="sha256:" + "ab" * 32,
        decode_fingerprint="sha256:" + "cd" * 32,
        lanes={"beatgrid": LaneResult(status="ok", payload=payload)},
    )
    conn = sqlite3.connect(str(state_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS analysis (
            stable_id TEXT, backend TEXT, backend_version TEXT,
            analyzed_at TEXT, duration_s REAL, sample_rate INTEGER,
            bpm REAL, bpm_confidence REAL, key_camelot TEXT, key_openkey TEXT,
            key_confidence REAL, energy INTEGER, energy_source TEXT,
            record_json TEXT,
            PRIMARY KEY (stable_id, backend, backend_version)
        );
        CREATE TABLE IF NOT EXISTS analysis_canonical (
            stable_id TEXT, lane TEXT, backend TEXT, backend_version TEXT,
            updated_at TEXT, PRIMARY KEY (stable_id, lane)
        );
        """
    )
    now = datetime.now(UTC).isoformat()
    record_json = record.to_json()
    conn.execute(
        """
        INSERT OR REPLACE INTO analysis (
            stable_id, backend, backend_version, analyzed_at, duration_s,
            sample_rate, bpm, bpm_confidence, key_camelot, key_openkey,
            key_confidence, energy, energy_source, record_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stable_id,
            record.backend,
            record.backend_version,
            now,
            record.duration_s,
            record.sample_rate,
            record.bpm,
            record.bpm_confidence,
            record.key_camelot,
            record.key_openkey,
            record.key_confidence,
            record.energy,
            record.energy_source,
            record_json,
        ),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO analysis_canonical
        (stable_id, lane, backend, backend_version, updated_at)
        VALUES (?, 'beatgrid', ?, ?, ?)
        """,
        (stable_id, record.backend, record.backend_version, now),
    )
    conn.commit()
    conn.close()


def _pin_reversal_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    from apps.sync import analysis_writeback

    reversal = tmp_path / "reversal"
    orig = analysis_writeback.LiveWriteSession

    def _session(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("reversal_root") is None:
            kwargs["reversal_root"] = reversal
        return orig(*args, **kwargs)

    monkeypatch.setattr(analysis_writeback, "LiveWriteSession", _session)
    return reversal


def _analysis_preimage_path(
    reversal: Path,
    *,
    dat_path: Path | None = None,
    content_id: str | None = None,
) -> Path:
    matches = list(reversal.rglob("analysis_preimages.json"))
    assert matches, "live write did not record analysis_preimages.json"
    if dat_path is not None:
        resolved = dat_path.resolve()
        for path in matches:
            snapshots = json.loads(path.read_text(encoding="utf-8"))
            for snap in snapshots:
                if snap.get("field") != "pqtz":
                    continue
                if Path(str(snap["dat_path"])).resolve() == resolved:
                    return path
        raise AssertionError(f"no analysis_preimages.json names pqtz dat_path {dat_path}")
    if content_id is not None:
        for path in matches:
            snapshots = json.loads(path.read_text(encoding="utf-8"))
            for snap in snapshots:
                if str(snap.get("content_id")) == content_id:
                    return path
        raise AssertionError(f"no analysis_preimages.json names content_id {content_id}")
    assert len(matches) == 1, f"expected one analysis_preimages.json under {reversal}, got {matches}"
    return matches[0]


def _stamp_writeback(
    state_path: Path,
    rb_path: Path,
    lanes: tuple[str, ...],
    fields: tuple[str, ...],
) -> None:
    from apps.sync.analysis_writeback import build_writeback_plan, writable_plan_hash
    from apps.sync.safety import mark_writeback_plan

    plan = build_writeback_plan(
        state_db=state_path,
        rb_db_path=rb_path,
        lanes=lanes,
        fields=fields,
        only_tracks=None,
    )
    mark_writeback_plan("apply_analysis", writable_plan_hash(plan))


def _key_camelot(conn: sqlite3.Connection, content_id: str) -> str:
    from apps.shared.harmonic import key_to_camelot

    row = conn.execute(
        """
        SELECT k.ScaleName
        FROM djmdContent c
        LEFT JOIN djmdKey k ON c.KeyID = k.ID
        WHERE c.ID = ?
        """,
        (content_id,),
    ).fetchone()
    assert row and row[0]
    return str(key_to_camelot(str(row[0])))
