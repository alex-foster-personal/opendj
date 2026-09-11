"""Regressions for review round nine on PR #1549.

[if] any round-nine review finding on PR #1549 recurs [then] fail, [else stop].

Third file in this series only because the second crossed the 600-line
per-Python-file limit; same discipline as the other two -- the claim was RUN
and the wrong behavior observed before anything changed, and each fix goes
red under a mutation back to its pre-fix source.

-Claude
"""
from __future__ import annotations

import sqlite3

import pytest

from apps.analysis import store as analysis_store
from apps.analysis.lanes import LaneContractError, LaneResult
from apps.shared.state import db as state_db
from tests.analysis_contract.conftest import key_payload, own_record

pytestmark = pytest.mark.requirement("NATIVE-04")

STAMP = "2026-01-01T00:00:00Z"


def _app_on(path):
    """An app whose SERVING backend is the same database the endpoint writes.

    `create_app()` defaults to InMemoryBackend, and the endpoint now refuses
    to persist a lane default on one -- a promotion the running backend
    cannot read would report `own` while /tracks kept serving rekordbox
    (Codex P1). These tests want the real pairing, so they wire it.
    """
    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import SqliteBackend

    app = create_app()
    app.state.analysis_db_path = path
    app.state.backend = SqliteBackend(path)
    return app


def _bare_db(tmp_path):
    """A state.db with the Phase 5 schema and NO analysis tables at all."""
    path = tmp_path / "state.db"
    state_db.open_rw(path).close()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES ('t1', 'inferred', 'x', ?, ?)", (STAMP, STAMP),
    )
    conn.execute(
        "INSERT INTO track_fields (stable_id, field_name, value_json, source, "
        "confidence, modified_at) VALUES ('t1', 'key', '\"5A\"', 'rekordbox', 1.0, ?)",
        (STAMP,),
    )
    conn.commit()
    return path, conn


#-----------------------------------------------------------------------------
# P2 round 9: a key the deck cannot parse is not a successful measurement
#-----------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["13Z", "13A", "0A", "8C", "8", "A8", "8a "])
def test_a_camelot_key_the_deck_cannot_parse_is_refused(db, bad: str) -> None:
    """`parseCamelotKey` returns null for these, silently disabling Key Sync."""
    payload = key_payload()
    payload["camelot"] = bad
    with pytest.raises(LaneContractError, match="Camelot"):
        analysis_store.upsert_record(
            own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
            conn=db,
        )


def test_the_three_spellings_of_one_key_must_agree(db) -> None:
    """8A is A minor. A record saying otherwise is not a rounding difference."""
    payload = key_payload(camelot="8A")
    payload["pitch_class"] = 0  # C, not A
    with pytest.raises(LaneContractError, match="pitch class"):
        analysis_store.upsert_record(
            own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
            conn=db,
        )
    payload = key_payload(camelot="8A")
    payload["is_minor"] = False
    with pytest.raises(LaneContractError, match="is_minor"):
        analysis_store.upsert_record(
            own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
            conn=db,
        )


def test_the_camelot_table_matches_the_decks_own(db) -> None:
    """The control that matters: this table is a TRANSCRIPTION, so prove it.

    `CAMELOT_ROOTS` in apps/webui/frontend/src/lib/player/key/camelot.ts is
    the authority the deck reads keys with. A table that disagreed with it
    would reject valid keys or accept ones the deck cannot use -- and my own
    first draft of these anchors was wrong, so this is read from the file
    rather than from memory.
    """
    import re as _re
    from pathlib import Path as _Path

    from apps.analysis.lane_payloads import _CAMELOT_PITCH_CLASS

    source = (
        _Path(__file__).resolve().parents[2]
        / "apps/webui/frontend/src/lib/player/key/camelot.ts"
    ).read_text()
    block = source[source.index("const CAMELOT_ROOTS"):]
    minor = [int(x) for x in _re.search(r"A: \[([\d, ]+)\]", block).group(1).split(",")]
    major = [int(x) for x in _re.search(r"B: \[([\d, ]+)\]", block).group(1).split(",")]
    assert len(minor) == len(major) == 12

    for n, pitch_class in enumerate(minor, start=1):
        assert _CAMELOT_PITCH_CLASS[f"{n}A"] == (pitch_class, True)
    for n, pitch_class in enumerate(major, start=1):
        assert _CAMELOT_PITCH_CLASS[f"{n}B"] == (pitch_class, False)


#-----------------------------------------------------------------------------
# P2 round 9: a source read, and a toggle, must not create state.db
#-----------------------------------------------------------------------------

def test_the_source_get_answers_when_state_db_does_not_exist(tmp_path) -> None:
    """`mode=ro` raises on an absent file, which is exactly the fresh-daemon case."""
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    path = tmp_path / "does-not-exist.db"
    app = create_app()
    app.state.analysis_db_path = path
    with TestClient(app) as client:
        resp = client.get("/api/v1/analysis/source")
    assert resp.status_code == 200, resp.text
    assert resp.json()["lanes"]["key"]["effective"] == "rbx"
    assert not path.exists(), "a GET must not create the database"


def test_a_toggle_only_put_does_not_create_state_db(tmp_path) -> None:
    """Creating it would flip `make_backend()` to SqliteBackend on NEXT launch.

    A session-only developer toggle would then permanently replace the
    in-memory library with an empty SQLite one -- a durable consequence from
    the one control explicitly documented as not having any.
    """
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    path = tmp_path / "state.db"
    app = create_app()
    app.state.analysis_db_path = path
    with TestClient(app) as client:
        resp = client.put(
            "/api/v1/analysis/source", json={"lane": "key", "toggle": "own"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["lanes"]["key"]["effective"] == "own"
    assert not path.exists(), "a toggle-only PUT must not create the database"
    from apps.analysis import selection as _selection

    _selection.reset_toggles()
