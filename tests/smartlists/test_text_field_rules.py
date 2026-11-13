"""Smartlist text rules: title, artist, album, comments (issue #10022, rekordbox text conditions mimicked).

[if] a text field rule is written [then] the schema accepts it and the evaluator matches it case-insensitively, [else stop].
"""

from __future__ import annotations

import json
import socket
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uvicorn

from apps.opendj_cli import api_cli
from apps.opendj_cli.__main__ import main
from apps.shared.pairings.schema_sql import ensure_phase08_tables
from apps.shared.smartlists import SmartlistRuleError, validate_rule
from apps.shared.smartlists.schema import ALLOWED_OPS_BY_FIELD, FIELD_TYPES
from apps.shared.state import db as state_db
from apps.smartlists.evaluator import evaluate
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.waits import start_uvicorn_in_thread

TEXT_FIELDS = ("title", "artist", "album", "comments")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _insert_track(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    title: str | None = None,
    artists: list[str] | None = None,
    album: str | None = None,
    comments: str | None = None,
) -> None:
    ts = _now()
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, album, created_at, updated_at) "
        "VALUES (?, 'isrc', ?, ?, ?, ?, ?)",
        (stable_id, title, None if artists is None else json.dumps(artists), album, ts, ts),
    )
    if comments is not None:
        conn.execute(
            "INSERT INTO track_fields (stable_id, field_name, value_json, source, modified_at) "
            "VALUES (?, 'comments', ?, 'rekordbox', ?)",
            (stable_id, json.dumps(comments), ts),
        )


@pytest.fixture
def text_library(state_conn: sqlite3.Connection) -> sqlite3.Connection:
    _insert_track(state_conn, "glue", title="Glue", artists=["Bicep"], album="Bicep", comments="peak time banger")
    _insert_track(
        state_conn, "collab", title="  Apricots ", artists=["Hammer", "BICEP"], album="Isles", comments="Warm up"
    )
    _insert_track(state_conn, "solo", title="Opal", artists=["Four Tet"], album=None, comments=None)
    _insert_track(state_conn, "blank", title="100% Pure_Love", artists=[], album="   ", comments="")
    return state_conn


def _match(conn: sqlite3.Connection, field: str, op: str, value: object) -> set[str]:
    return set(evaluate({"field": field, "op": op, "value": value}, conn))


# ----------------------------------------------------------------------------
# schema
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_text_fields_are_strings_with_string_ops(field: str) -> None:
    """[if] title/artist/album/comments are read from the schema [then] each is a string field with string ops, [else stop]."""
    assert FIELD_TYPES[field] == "string"
    assert set(ALLOWED_OPS_BY_FIELD[field]) == {"=", "!=", "contains", "in", "missing"}
    validate_rule({"field": field, "op": "contains", "value": "x"})
    with pytest.raises(SmartlistRuleError):
        validate_rule({"field": field, "op": ">", "value": "x"})


# ----------------------------------------------------------------------------
# evaluator
# ----------------------------------------------------------------------------


def test_contains_and_equals_are_case_insensitive_and_trimmed(text_library: sqlite3.Connection) -> None:
    """[if] contains or = runs on a text field [then] case and surrounding whitespace are ignored, [else stop]."""
    assert _match(text_library, "title", "=", " apricots  ") == {"collab"}
    assert _match(text_library, "title", "contains", "GLU") == {"glue"}
    assert _match(text_library, "album", "=", "BICEP") == {"glue"}
    assert _match(text_library, "comments", "contains", " PEAK ") == {"glue"}
    assert _match(text_library, "comments", "in", ["warm UP", "nothing"]) == {"collab"}


def test_contains_treats_like_wildcards_literally(text_library: sqlite3.Connection) -> None:
    """[if] a contains value holds % or _ [then] they match literally, not as SQL wildcards, [else stop]."""
    assert _match(text_library, "title", "contains", "100%") == {"blank"}
    assert _match(text_library, "title", "contains", "e_l") == {"blank"}
    assert _match(text_library, "title", "contains", "%") == {"blank"}


def test_artist_matches_any_credited_artist(text_library: sqlite3.Connection) -> None:
    """[if] artist = or contains Bicep [then] tracks crediting Bicep anywhere in artists_json match, [else stop]."""
    assert _match(text_library, "artist", "=", "bicep") == {"glue", "collab"}
    assert _match(text_library, "artist", "contains", "four") == {"solo"}
    assert _match(text_library, "artist", "in", ["hammer"]) == {"collab"}
    assert _match(text_library, "artist", "!=", "Bicep") == {"solo", "blank"}


def test_missing_selects_null_and_blank_values(text_library: sqlite3.Connection) -> None:
    """[if] album missing runs on a fixture with a null album [then] the null and blank album tracks match, [else stop]."""
    assert _match(text_library, "album", "missing", None) == {"solo", "blank"}
    assert _match(text_library, "comments", "missing", None) == {"solo", "blank"}
    assert _match(text_library, "artist", "missing", None) == {"blank"}


def test_not_equals_keeps_tracks_without_a_value(text_library: sqlite3.Connection) -> None:
    """[if] album != Bicep runs [then] tracks with other, null or blank albums match, [else stop]."""
    assert _match(text_library, "album", "!=", "bicep") == {"collab", "solo", "blank"}


# ----------------------------------------------------------------------------
# agent parity: opendj api create + GET /smartlists/{id}/tracks
# ----------------------------------------------------------------------------


@pytest.fixture
def library_daemon(tmp_path: Path) -> Iterator[str]:
    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path, apply_schema=True)
    try:
        ensure_phase08_tables(conn)
        _insert_track(conn, "glue", title="Glue", artists=["Bicep"], album="Bicep")
        _insert_track(conn, "collab", title="Apricots", artists=["Hammer", "Bicep"], album=None)
        _insert_track(conn, "solo", title="Opal", artists=["Four Tet"], album="Opal")
        conn.commit()
    finally:
        conn.close()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    app = create_app(
        backend=SqliteBackend(db_path), state_db_path=str(db_path), mount_frontend=False, enable_cors=False
    )
    server, thread = start_uvicorn_in_thread(
        uvicorn.Config(app, log_level="warning"), what="the library daemon", sockets=[listener]
    )
    try:
        yield f"http://127.0.0.1:{int(listener.getsockname()[1])}"
    finally:
        server.should_exit = True
        thread.join(timeout=0.5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=10)


def test_opendj_api_text_rule_round_trips_through_tracks_route(
    library_daemon: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] opendj api POSTs an artist contains rule [then] GET /smartlists/{id}/tracks returns the matching tracks, [else stop]."""
    monkeypatch.setattr(
        api_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(base_url=library_daemon),
    )
    rule = {
        "op": "and",
        "children": [
            {"field": "artist", "op": "contains", "value": "BICEP"},
            {"field": "album", "op": "missing", "value": None},
        ],
    }
    body = json.dumps({"name": "Bicep, no album", "rule": rule})
    assert main(["api", "POST", "/api/v1/smartlists", "--json", body]) == api_cli.EXIT_OK
    created = json.loads(capsys.readouterr().out)
    assert created["rule"] == rule

    assert main(["api", "GET", f"/api/v1/smartlists/{created['id']}/tracks"]) == api_cli.EXIT_OK
    tracks = json.loads(capsys.readouterr().out)["tracks"]
    assert [row["stable_id"] for row in tracks] == ["collab"]
