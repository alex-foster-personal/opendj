"""key_openkey backfill: re-derive from key_camelot via canon, never a pattern."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.analysis_key import canon
from scripts.migrate_key_openkey import apply, dry_run, main, rewrite_record_json

_SCHEMA = """
CREATE TABLE analysis (
    stable_id        TEXT NOT NULL,
    backend          TEXT NOT NULL,
    backend_version  TEXT NOT NULL,
    analyzed_at      TEXT NOT NULL,
    duration_s       REAL NOT NULL,
    sample_rate      INTEGER NOT NULL,
    bpm              REAL NOT NULL,
    bpm_confidence   REAL NOT NULL,
    key_camelot      TEXT NOT NULL,
    key_openkey      TEXT NOT NULL,
    key_confidence   REAL NOT NULL,
    energy           INTEGER NOT NULL,
    energy_source    TEXT NOT NULL,
    record_json      TEXT NOT NULL,
    PRIMARY KEY (stable_id, backend, backend_version)
)
"""


def _record_json(camelot: str, openkey: str) -> str:
    return json.dumps(
        {
            "key_camelot": camelot,
            "key_openkey": openkey,
            "lanes": {
                "key": {
                    "status": "ok",
                    "payload": {
                        "camelot": camelot,
                        "openkey": openkey,
                        "segments": {
                            "status": "ok",
                            "reason": None,
                            "segments": [
                                {
                                    "start_bar": 0,
                                    "end_bar": 8,
                                    "start_s": 0.0,
                                    "end_s": 16.0,
                                    "key_camelot": camelot,
                                    "key_openkey": openkey,
                                    "confidence": 0.9,
                                }
                            ],
                        },
                    },
                }
            },
        }
    )


def _insert(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    camelot: str,
    openkey: str,
    backend: str = "librosa-only",
) -> None:
    conn.execute(
        """
        INSERT INTO analysis (
            stable_id, backend, backend_version, analyzed_at, duration_s,
            sample_rate, bpm, bpm_confidence, key_camelot, key_openkey,
            key_confidence, energy, energy_source, record_json
        ) VALUES (?, ?, '1.0.0', '2026-09-01T00:00:00Z', 180, 44100, 120, 0.9,
                  ?, ?, 0.8, 5, 'inferred', ?)
        """,
        (stable_id, backend, camelot, openkey, _record_json(camelot, openkey)),
    )


def _seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    _insert(conn, stable_id="ok-8a", camelot="8A", openkey="1m")
    _insert(conn, stable_id="rot-8a", camelot="8A", openkey="8m")
    _insert(conn, stable_id="rot-1a", camelot="1A", openkey="1m")
    _insert(conn, stable_id="rot-12a", camelot="12A", openkey="12m")
    _insert(conn, stable_id="bad", camelot="not-a-key", openkey="??")
    conn.commit()
    conn.close()
    return db


def _fetch(db: Path, stable_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT key_camelot, key_openkey, record_json FROM analysis WHERE stable_id=?",
        (stable_id,),
    ).fetchone()
    conn.close()
    return row


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    stats = dry_run(db)
    assert stats["librosa-only"]["n_total"] == 5
    assert stats["librosa-only"]["n_ok"] == 1
    assert stats["librosa-only"]["n_affected"] == 3
    assert stats["librosa-only"]["n_unparseable"] == 1
    still = _fetch(db, "rot-8a")
    assert still["key_openkey"] == "8m"


def test_apply_rederives_from_canon_not_a_hardcoded_map(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    apply(db)
    ok = _fetch(db, "ok-8a")
    assert ok["key_openkey"] == "1m"
    for sid, camelot in (("rot-8a", "8A"), ("rot-1a", "1A"), ("rot-12a", "12A")):
        row = _fetch(db, sid)
        expected = canon.to_open_key(canon.from_camelot(camelot))
        assert row["key_openkey"] == expected
        blob = json.loads(row["record_json"])
        assert blob["key_openkey"] == expected
        payload = blob["lanes"]["key"]["payload"]
        assert payload["openkey"] == expected
        assert payload["segments"]["segments"][0]["key_openkey"] == expected
    bad = _fetch(db, "bad")
    assert bad["key_openkey"] == "??"


def test_rewrite_record_json_round_trips_8a() -> None:
    expected = canon.to_open_key(canon.from_camelot("8A"))
    assert expected == "1m"
    rewritten = rewrite_record_json(_record_json("8A", "8m"), expected)
    data = json.loads(rewritten)
    assert data["key_openkey"] == "1m"
    assert data["lanes"]["key"]["payload"]["openkey"] == "1m"


def test_no_analysis_table_exits_nonzero(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    with pytest.raises(SystemExit, match="no analysis table"):
        main(["--db", str(db)])
