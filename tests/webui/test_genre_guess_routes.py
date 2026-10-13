"""The web UI serves JEV genre guesses: library rows, search and lyric search.

Moved from (private path): these drive `create_app`, so they are web UI
suites, and a change under apps/webui/ selects them here rather than making genre_infer a
dependent of webui (SMARTEST-CI round 16).

- [if] an untagged track's row or search hit is served [then] it carries its JEV guess and a tagged track's does not, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.genre_infer import __main__ as cli
from apps.genre_infer import jev
from tests.genre_infer.jev_support import WARMUP, captured, library

pytestmark = pytest.mark.requirement("GENRE-02")


def test_rows_read_guesses_from_a_flat_layout_library(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import SqliteBackend

    _data, nested = library(tmp_path / "nested")
    # A flat library keeps its database directly in the data root.
    root = tmp_path / "flat"
    root.mkdir()
    state = root / "library.db"
    state.write_bytes(nested.read_bytes())
    answer = jev.parse_answer(captured("dnb_with_tag")["response"], [WARMUP])
    cli._jev_write(root, {"t-bare": answer}, [WARMUP], 0.8)
    app = create_app(
        backend=SqliteBackend(state),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state),
        mount_frontend=False,
    )
    with TestClient(app) as client:
        resp = client.get("/api/v1/tracks")
    assert resp.status_code == 200, resp.text
    rows = {r["stable_id"]: r for r in resp.json()["items"]}
    assert rows["t-bare"]["genre_guess"]["family"] == "drum-and-bass"


def test_lyric_search_reads_guesses_from_a_flat_layout_library(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from apps.lyrics.cache import LyricLine, Lyrics, cache_path, write
    from apps.lyrics.search_index import index_batch
    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import SqliteBackend

    _data, nested = library(tmp_path / "nested")
    # Flat library: the database sits directly in its data root. The lyric index
    # keeps its own (two levels up) location; the guess must come from the library.
    root = tmp_path / "flat" / "lib"
    root.mkdir(parents=True)
    state = root / "library.db"
    state.write_bytes(nested.read_bytes())
    lyric_root = state.parent.parent
    write(
        cache_path(lyric_root, "t-bare"),
        Lyrics(stable_id="t-bare", source="lrclib", lines=(LyricLine(start_ms=0, text="moonlight drums"),)),
    )
    index_batch(lyric_root)
    answer = jev.parse_answer(captured("dnb_with_tag")["response"], [WARMUP])
    cli._jev_write(root, {"t-bare": answer}, [WARMUP], 0.8)
    app = create_app(
        backend=SqliteBackend(state),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state),
        mount_frontend=False,
    )
    with TestClient(app) as client:
        resp = client.get("/api/v1/lyrics/search", params={"q": "moonlight"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["stable_id"] for i in items] == ["t-bare"]
    assert items[0]["genre_guess"]["family"] == "drum-and-bass"


def test_library_rows_serve_the_guess_only_for_untagged_tracks(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import SqliteBackend

    data, state = library(tmp_path)
    answer = jev.parse_answer(captured("dnb_with_tag")["response"], [WARMUP])
    # Written by the CLI's own writer; a guess for a TAGGED track must never be served over its tag.
    cli._jev_write(data, {"t-tagged": answer, "t-bare": answer, "t-blank": answer}, [WARMUP], 0.8)
    # No config patching: the app is built against this library alone, so the
    # rows must read the sidecar of its configured state_db_path, never the
    # process-global data root.
    app = create_app(
        backend=SqliteBackend(state),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state),
        mount_frontend=False,
    )
    with TestClient(app) as client:
        resp = client.get("/api/v1/tracks")
        found = client.get("/api/v1/search", params={"q": "Bare"})
    assert resp.status_code == 200, resp.text
    rows = {r["stable_id"]: r for r in resp.json()["items"]}
    assert rows["t-bare"]["genre_guess"] == {
        "family": "drum-and-bass",
        "confidence": answer["confidence"],
        "source": "jev",
    }
    assert rows["t-blank"]["genre_guess"] is not None
    # Search hydrates rows through the same builder and the same configured library.
    assert found.status_code == 200, found.text
    assert [r["genre_guess"]["family"] for r in found.json()["items"]] == ["drum-and-bass"]
    assert rows["t-tagged"]["genre"] == "House"
    assert rows["t-tagged"]["genre_guess"] is None


def test_a_tag_hidden_by_the_genre_precedence_still_suppresses_the_guess(tmp_path: Path) -> None:
    """[if] the precedence hides a track's library tag [then] the row shows no genre and still no guess, [else stop]."""
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import SqliteBackend

    data, state = library(tmp_path)
    answer = jev.parse_answer(captured("dnb_with_tag")["response"], [WARMUP])
    cli._jev_write(data, {"t-tagged": answer, "t-bare": answer}, [WARMUP], 0.8)
    app = create_app(
        backend=SqliteBackend(state),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state),
        mount_frontend=False,
    )
    with TestClient(app) as client:
        before = {r["stable_id"]: r for r in client.get("/api/v1/tracks").json()["items"]}
        assert before["t-tagged"]["genre"] == "House" and before["t-tagged"]["genre_guess"] is None
        assert client.put("/api/v1/genres/config", json={"precedence": ["beatport"]}).status_code == 200
        rows = {r["stable_id"]: r for r in client.get("/api/v1/tracks").json()["items"]}
    assert rows["t-tagged"]["genre"] is None
    assert rows["t-tagged"]["genre_guess"] is None
    # Control: an untagged track still gets its guess under the same precedence.
    assert rows["t-bare"]["genre_guess"] is not None
