"""LIBUX-13824: the Bitrate and Kind columns read the quality data every hydrated row already carries (issue #9735).

- [if] a playlist row lacks quality.kbps or quality.container [then] agents cannot read Bitrate or Kind, [else stop].
- [if] a track with no file on disk carries a guessed kbps [then] the Bitrate cell shows a made-up rate, [else stop].

The hidden-columns pref (bitrate and kind hidden by default, PUT-able) is proven in
tests/webui/test_ui_prefs_library_hidden_columns.py; the cell text and sort order in
apps/webui/frontend/tests/unit/library-bitrate-kind-columns.test.mjs.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

ON_DISK = "a" * 40  # a real 40 kB .flac file, 1 s long
MISSING = "b" * 40  # an .mp3 path with no file behind it
DURATION_MS = 1000


def _make_state_db(path: Path, audio: Path) -> None:
    conn = state_db.open_rw(path)
    try:
        for sid, title, file_path in (
            (ON_DISK, "Harbor", str(audio)),
            (MISSING, "Lumen", str(path.parent / "gone.mp3")),
        ):
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, file_path, "
                "created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, '2026-11-13', '2026-11-13')",
                (sid, title, DURATION_MS, file_path),
            )
        conn.execute(
            "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) "
            "VALUES ('pl-q', 'Q', 'rekordbox', 'rb-pl-1', '2026-11-13', '2026-11-13')"
        )
        for position, sid in enumerate((ON_DISK, MISSING)):
            conn.execute(
                "INSERT INTO playlist_memberships (playlist_id, stable_id, position) VALUES ('pl-q', ?, ?)",
                (sid, position),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    audio = tmp_path / "harbor.flac"
    audio.write_bytes(b"\0" * 40_000)
    state_path = tmp_path / "state.db"
    _make_state_db(state_path, audio)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.master.plain.db")
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_path),
        mount_frontend=False,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.requirement("LIBUX-13824")
def test_playlist_rows_carry_bitrate_and_kind_data(client: TestClient) -> None:
    """[if] GET /playlists/{id} is read [then] rows carry measured kbps and the container, null kbps when missing, [else stop]."""
    body = client.get("/api/v1/playlists/pl-q").json()
    quality = {row["stable_id"]: row["quality"] for row in body["tracks"]}
    assert quality[ON_DISK]["kbps"] == 320
    assert quality[ON_DISK]["container"] == ".flac"
    assert quality[MISSING]["kbps"] is None
    assert quality[MISSING]["container"] == ".mp3"
