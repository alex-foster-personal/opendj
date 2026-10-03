"""LIBUX-16: materialize staged ingest batch into state.db (issue #3182).

[if] a staged batch contains new audio [then] materialize returns stable_ids, [else stop].
[if] an exact duplicate is skipped and a new file staged [then] both resolve, [else stop].
[if] any staged file yields no track row [then] nothing is committed, [else stop].
[if] nested files share a basename [then] they stay distinct tracks, [else stop].
[if] a dropped file is a track the user removed [then] it is added again, [else stop] (LIBM-141).
"""
from __future__ import annotations

import sqlite3
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.paths import AUDIO_EXTENSIONS
from apps.shared.state.db import open_rw as open_state_rw
from apps.webui.server.routes import ingest as ingest_mod
from apps.webui.server.routes import ingest_materialize as ingest_materialize_mod
from apps.webui.server.routes import ingest_upload as ingest_upload_mod

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"

pytestmark = pytest.mark.requirement("LIBUX-16")


@pytest.fixture
def app(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    state_db = data_dir / "state" / "state.db"
    open_state_rw(state_db).close()
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))

    monkeypatch.setattr(ingest_mod, "CONFIG_PATH", tmp_path / "ingest-config.json")
    monkeypatch.setattr(ingest_mod, "INGEST_INBOX", tmp_path / "_ingest")
    monkeypatch.setattr(ingest_mod, "open_ro", lambda: sqlite3.connect(state_db))

    app = FastAPI()
    app.include_router(ingest_upload_mod.router, prefix="/api/v1")
    app.include_router(ingest_materialize_mod.router, prefix="/api/v1")
    app.state.state_db = state_db
    app.state.state_db_path = state_db
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.mark.requires_audio_stack
def test_materialize_returns_stable_ids(client, app):
    src = FIXTURES / "src-128.mp3"
    up = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (src.name, src.read_bytes(), "audio/mpeg"))],
        data={"batch": "agnes"},
    )
    assert up.status_code == 200
    batch = up.json()["batch"]

    mat = client.post(f"/api/v1/ingest/batch/{batch}/materialize")
    assert mat.status_code == 200
    body = mat.json()
    assert body["batch"] == batch
    assert len(body["tracks"]) == 1
    assert body["tracks"][0]["relative_path"] == src.name
    assert body["tracks"][0]["inserted"] is True
    stable_id = body["tracks"][0]["stable_id"]
    assert stable_id

    conn = sqlite3.connect(app.state.state_db)
    row = conn.execute(
        "SELECT stable_id FROM tracks WHERE stable_id = ?", (stable_id,)
    ).fetchone()
    conn.close()
    assert row is not None


def _upload(client, batch, sources):
    """POST real fixture bytes to the production upload route."""
    return client.post(
        "/api/v1/ingest/upload",
        files=[
            ("files", (name, src.read_bytes(), "audio/mpeg")) for name, src in sources
        ],
        data={"batch": batch},
    )


def _track_rows_under(app, root: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(app.state.state_db)
    try:
        return conn.execute(
            "SELECT stable_id, file_path FROM tracks WHERE file_path LIKE ?",
            (f"{root.resolve()}%",),
        ).fetchall()
    finally:
        conn.close()


@pytest.mark.requires_audio_stack
@pytest.mark.requires_fpcalc
def test_materialize_mixed_new_and_exact_duplicate(client, app):
    # No stubbed fingerprinting: the duplicate is made a library track through
    # the production upload + materialize path (a first folder drop), then the
    # second drop fingerprints it for real with chromaprint.
    dup_src = FIXTURES / "other-silent-intro.mp3"
    new_src = FIXTURES / "src-128.mp3"

    first = _upload(client, "first-drop", [(dup_src.name, dup_src)])
    assert first.status_code == 200
    assert first.json()["results"][0]["verdict"] == "new"
    first_mat = client.post("/api/v1/ingest/batch/first-drop/materialize")
    assert first_mat.status_code == 200
    dup_id = first_mat.json()["tracks"][0]["stable_id"]

    up = _upload(
        client,
        "mixed-folder",
        [(dup_src.name, dup_src), (new_src.name, new_src)],
    )
    assert up.status_code == 200
    results = up.json()["results"]
    dup_row = next(r for r in results if r["filename"] == dup_src.name)
    new_row = next(r for r in results if r["filename"] == new_src.name)
    assert dup_row["verdict"] == "skipped_duplicate"
    assert dup_row["duplicate_of"]["stable_id"] == dup_id
    assert dup_row["duplicate_of"]["method"] == "chromaprint"
    assert new_row["verdict"] == "new"

    mat = client.post("/api/v1/ingest/batch/mixed-folder/materialize")
    assert mat.status_code == 200
    tracks = {t["relative_path"]: t for t in mat.json()["tracks"]}
    assert set(tracks) == {new_src.name}
    assert tracks[new_src.name]["inserted"] is True
    assert tracks[new_src.name]["stable_id"] != dup_id

    dest = Path(up.json()["dest_dir"])
    staged = [
        p
        for p in dest.rglob("*")
        if p.is_file()
        and not p.name.endswith(".part")
        and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    assert [p.name for p in staged] == [new_src.name]


def _write_header_only_wav(path: Path) -> None:
    """A RIFF/WAVE file with no PCM frames: an allowlisted extension over an
    unplayable payload, which upload refuses and the folder adapter rejects."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"")


@pytest.mark.requires_audio_stack
def test_materialize_commits_nothing_when_a_staged_file_is_unplayable(
    client, app, tmp_path
):
    good = FIXTURES / "src-128.mp3"
    broken = tmp_path / "broken.wav"
    _write_header_only_wav(broken)

    # Upload refuses a file with no audio frames before it is staged (LIBMX-15).
    refused = _upload(client, "header-only", [(broken.name, broken)])
    assert refused.status_code == 422, refused.text

    # A file can still turn unplayable after it staged (damaged or replaced on
    # disk before materialize), so stage a real wav and then damage it there.
    up = _upload(
        client, "mixed-broken", [(good.name, good), (broken.name, FIXTURES / "src.wav")]
    )
    assert up.status_code == 200, up.text
    verdicts = {r["filename"]: r["verdict"] for r in up.json()["results"]}
    assert verdicts == {good.name: "new", broken.name: "new"}
    dest = Path(up.json()["dest_dir"])
    staged = dest / broken.name
    assert staged.is_file()
    _write_header_only_wav(staged)

    mat = client.post("/api/v1/ingest/batch/mixed-broken/materialize")
    assert mat.status_code == 422
    detail = mat.json()["detail"]
    assert detail["code"] == "MATERIALIZE_UNRESOLVED"
    assert detail["unresolved"] == [broken.name]
    # The playable peer must NOT be durable: the failure is all-or-nothing.
    assert _track_rows_under(app, dest) == []


@pytest.mark.requires_audio_stack
def test_materialize_nested_same_basename_files_stay_distinct(client, app):
    # A multi-disc folder: two files share a basename in different subfolders.
    # The folder walker sends each as its path relative to the drop root, so
    # both stage and materialize as distinct tracks (no 409 collision).
    a = FIXTURES / "src-128.mp3"
    b = FIXTURES / "other-silent-intro.mp3"
    up = _upload(
        client,
        "multi-disc",
        [("Album/Disc 1/01.mp3", a), ("Album/Disc 2/01.mp3", b)],
    )
    assert up.status_code == 200, up.text
    names = [r["filename"] for r in up.json()["results"]]
    assert names == ["Album/Disc 1/01.mp3", "Album/Disc 2/01.mp3"]

    mat = client.post("/api/v1/ingest/batch/multi-disc/materialize")
    assert mat.status_code == 200
    paths = sorted(t["relative_path"] for t in mat.json()["tracks"])
    assert paths == ["Album/Disc 1/01.mp3", "Album/Disc 2/01.mp3"]
    assert len({t["stable_id"] for t in mat.json()["tracks"]}) == 2


@pytest.mark.requires_audio_stack
@pytest.mark.requirement("LIBM-141")
def test_materialize_adds_back_a_track_the_user_removed(client, app):
    """The e2e folder-drop sequence: drop, remove, drop the same file again."""
    from apps.shared.state.writer import StateWriter

    src = FIXTURES / "src-128.mp3"
    first = _upload(client, "first", [(src.name, src)])
    assert first.status_code == 200, first.text
    made = client.post(f"/api/v1/ingest/batch/{first.json()['batch']}/materialize")
    assert made.status_code == 200, made.text
    removed_id = made.json()["tracks"][0]["stable_id"]
    conn = open_state_rw(app.state.state_db)
    try:
        with StateWriter(conn, actor="test") as writer:
            writer.remove_from_library(removed_id)
    finally:
        conn.close()

    second = _upload(client, "second", [(src.name, src)])
    assert second.status_code == 200, second.text
    again = client.post(f"/api/v1/ingest/batch/{second.json()['batch']}/materialize")
    assert again.status_code == 200, again.text
    (track,) = again.json()["tracks"]
    conn = sqlite3.connect(app.state.state_db)
    try:
        live = conn.execute(
            "SELECT deleted_at FROM tracks WHERE stable_id = ?", (track["stable_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert live == (None,), "the dropped file is not a live track"
