"""Lyrics store + karaoke words endpoints against a real sqlite state DB."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.lyrics import artifacts, lines, store
from apps.lyrics.karaoke_cache import PIPELINE_VERSION
from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

from .conftest import use_local_mode, word

WORDS = [
    word("one", start_s=1.0, end_s=1.4, score=-0.5, witness="contradict", line_final=False),
    word("two", start_s=1.5, end_s=1.9, witness="agree"),
    word("three", start_s=2.0, end_s=2.4, witness="agree", line_final=True),
]


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    use_local_mode(monkeypatch)
    data_dir = tmp_path / "data"
    path = data_dir / "state" / "state.db"
    path.parent.mkdir(parents=True)
    conn = state_db.open_rw(path)
    sync_stamp.ensure_local_machine(conn)
    now = "2026-08-31T00:00:00+00:00"
    for sid, title in (("aaa", "Suspect Track"), ("bbb", "Clean Track")):
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
            "VALUES (?, 'inferred', ?, ?, ?)",
            (sid, title, now, now),
        )
    artifact = artifacts.produce_words_artifact(
        conn,
        data_dir=data_dir,
        stable_id="aaa",
        source="musixmatch matcher-get",
        words=WORDS,
        s3=None,
        cfg=None,
    )
    store.upsert_verdict(
        conn,
        stable_id="aaa",
        verdict="vocal",
        coverage_pct=48.0,
        source="musixmatch matcher-get",
        language_iso3="eng",
        n_words=artifact.n_words,
        n_lines=artifact.n_lines,
        pct_witness_red=0.9,
        pipeline_version=PIPELINE_VERSION,
        words_content_hash=artifact.content_hash,
        computed_at=now,
        resurrect=False,
    )
    store.upsert_verdict(
        conn,
        stable_id="bbb",
        verdict="no-lyrics",
        coverage_pct=3.0,
        source="musixmatch matcher-get",
        language_iso3="eng",
        n_words=0,
        n_lines=None,
        pct_witness_red=0.0,
        pipeline_version=PIPELINE_VERSION,
        words_content_hash=None,
        computed_at=now,
        resurrect=False,
    )
    conn.close()
    return path


@pytest.fixture()
def client(db_path: Path) -> TestClient:
    return TestClient(
        create_app(
            backend=SqliteBackend(db_path),
            bind_host="127.0.0.1",
            hostname="test-host",
            state_db_path=str(db_path),
            mount_frontend=False,
        )
    )


def _conn(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def test_override_beats_computed_verdict(db_path: Path) -> None:
    conn = _conn(db_path)
    assert store.get_verdict(conn, "bbb").effective == "no-lyrics"
    store.set_override(conn, stable_id="bbb", override="vocal", note="I hear a chant at 2:10")
    verdict = store.get_verdict(conn, "bbb")
    assert verdict.verdict == "no-lyrics"
    assert verdict.effective == "vocal"
    assert verdict.override_note == "I hear a chant at 2:10"


def test_recompute_preserves_an_existing_override(db_path: Path) -> None:
    conn = _conn(db_path)
    store.set_override(conn, stable_id="bbb", override="vocal", note=None)
    store.upsert_verdict(
        conn,
        stable_id="bbb",
        verdict="sparse",
        coverage_pct=14.0,
        source="musixmatch matcher-get",
        language_iso3="eng",
        n_words=0,
        n_lines=None,
        pct_witness_red=0.0,
        pipeline_version=PIPELINE_VERSION,
        words_content_hash=None,
        computed_at="2026-08-31T00:00:00+00:00",
        resurrect=False,
    )
    verdict = store.get_verdict(conn, "bbb")
    assert verdict.verdict == "sparse" and verdict.effective == "vocal"


def test_set_override_rejects_unknown_value(db_path: Path) -> None:
    conn = _conn(db_path)
    with pytest.raises(store.LyricStoreError):
        store.set_override(conn, stable_id="aaa", override="definitely-not-a-verdict", note=None)


def test_get_track_lyrics_words_returns_ordered_words(client: TestClient) -> None:
    response = client.get("/api/v1/tracks/aaa/lyrics/words")
    assert response.status_code == 200
    body = response.json()
    assert [w["word"] for w in body["words"]] == ["one", "two", "three"]
    assert [w["idx"] for w in body["words"]] == [0, 1, 2]
    assert body["words"][0]["witness"] == "contradict"
    assert body["verdict"]["effective"] == "vocal"


def test_missing_track_lyrics_words_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/tracks/nope/lyrics/words").status_code == 404


def test_triage_listing_puts_most_suspect_first(client: TestClient) -> None:
    rows = client.get("/api/v1/lyrics").json()
    assert [r["stable_id"] for r in rows] == ["aaa", "bbb"]


def test_summary_exposes_the_calibrated_band(client: TestClient) -> None:
    body = client.get("/api/v1/lyrics/summary").json()
    assert body["counts"] == {"vocal": 1, "no-lyrics": 1}
    assert body["total"] == 2
    assert body["no_lyrics_max_coverage"] == 12.5


def test_override_endpoint_round_trips_and_filters(client: TestClient) -> None:
    response = client.put(
        "/api/v1/tracks/bbb/lyrics/override",
        json={"override": "vocal", "note": "chant"},
    )
    assert response.status_code == 200
    assert response.json()["effective"] == "vocal"
    assert response.json()["verdict"] == "no-lyrics"
    listed = client.get("/api/v1/lyrics", params={"verdict": "vocal"}).json()
    assert {row["stable_id"] for row in listed} == {"aaa", "bbb"}
    cleared = client.put("/api/v1/tracks/bbb/lyrics/override", json={"override": None})
    assert cleared.json()["effective"] == "no-lyrics"


def test_override_endpoint_rejects_bogus_verdict(client: TestClient) -> None:
    response = client.put(
        "/api/v1/tracks/aaa/lyrics/override",
        json={"override": "banana"},
    )
    assert response.status_code == 422


def test_listing_carries_track_titles(client: TestClient) -> None:
    rows = client.get("/api/v1/lyrics").json()
    by_id = {r["stable_id"]: r for r in rows}
    assert by_id["aaa"]["title"] == "Suspect Track"
    assert by_id["bbb"]["title"] == "Clean Track"
    single = client.get("/api/v1/tracks/aaa/lyrics/words").json()
    assert single["verdict"]["title"] is None


def test_bulk_verdicts_is_one_query_shape(db_path: Path) -> None:
    conn = _conn(db_path)
    out = store.bulk_verdicts(conn, ["aaa", "bbb", "zzz-not-there"])
    assert set(out) == {"aaa", "bbb"}
    assert out["aaa"].n_words == 3
    assert store.bulk_verdicts(conn, []) == {}


def test_track_lyrics_words_include_lines(client: TestClient) -> None:
    plain = client.get("/api/v1/tracks/aaa/lyrics/words")
    assert plain.status_code == 200 and plain.json()["lines"] is None
    got = client.get("/api/v1/tracks/aaa/lyrics/words", params={"include": "lines"})
    assert got.status_code == 200
    line_rows = got.json()["lines"]
    assert len(line_rows) == 1
    assert line_rows[0]["text"] == "one two three"
    assert line_rows[0]["n_red"] == 1 and line_rows[0]["n_judged"] == 3
    assert line_rows[0]["band"] in ("good", "uncertain", "bad", "unjudged")
    assert client.get(
        "/api/v1/tracks/aaa/lyrics/words", params={"include": "bogus"}
    ).status_code == 422


def test_lyrics_config_roundtrip_and_validation(client: TestClient) -> None:
    got = client.get("/api/v1/lyrics/config")
    assert got.status_code == 200
    body = got.json()
    assert body["is_default"] is True
    order = body["source_order"]
    assert order[0].startswith("musixmatch")
    reordered = list(reversed(order))
    put = client.put("/api/v1/lyrics/config", json={"source_order": reordered})
    assert put.status_code == 200
    assert put.json()["source_order"] == reordered
    assert put.json()["is_default"] is False
    assert client.get("/api/v1/lyrics/config").json()["source_order"] == reordered
    partial = client.put("/api/v1/lyrics/config", json={"source_order": order[:2]})
    assert partial.status_code == 422
    unknown = client.put(
        "/api/v1/lyrics/config", json={"source_order": [*order, "spotify magic"]}
    )
    assert unknown.status_code == 422


def test_lyric_jobs_queue_validates_and_lists(client: TestClient) -> None:
    bad_kind = client.post(
        "/api/v1/lyrics/jobs", json={"kind": "teleport", "stable_ids": ["aaa"]}
    )
    assert bad_kind.status_code == 422
    too_many = client.post(
        "/api/v1/lyrics/jobs",
        json={"kind": "stems", "stable_ids": [f"sid-{i:04d}" for i in range(501)]},
    )
    assert too_many.status_code == 422
    assert too_many.json()["detail"] == "at most 500 tracks per job"
    bad_track = client.post(
        "/api/v1/lyrics/jobs", json={"kind": "stems", "stable_ids": ["nope"]}
    )
    assert bad_track.status_code == 422
    made = client.post(
        "/api/v1/lyrics/jobs",
        json={"kind": "lyricsync", "stable_ids": ["aaa", "bbb"], "note": "from test"},
    )
    assert made.status_code == 201
    job = made.json()
    assert job["status"] == "queued" and job["kind"] == "lyricsync"
    listed = client.get("/api/v1/lyrics/jobs").json()
    assert [j["id"] for j in listed] == [job["id"]]


def test_n_lines_on_verdict_matches_derive_lines(db_path: Path) -> None:
    conn = _conn(db_path)
    artifact = artifacts.load_words(
        conn,
        data_dir=db_path.parent.parent,
        stable_id="aaa",
        s3=None,
        cfg=None,
    )
    assert artifact is not None
    verdict = store.get_verdict(conn, "aaa")
    assert verdict is not None
    assert verdict.n_lines == len(lines.derive_lines(artifact.words))


def test_verdict_absence_detail_distinguishes_missing_and_tombstone(db_path: Path) -> None:
    conn = _conn(db_path)
    assert store.verdict_absence_detail(conn, "aaa") is None
    assert store.verdict_absence_detail(conn, "missing") == "no live lyric_verdict row for 'missing'"
    store.tombstone(conn, "bbb")
    assert store.verdict_absence_detail(conn, "bbb") == "lyric_verdict 'bbb' is tombstoned"


def test_purge_dry_run(client: TestClient) -> None:
    response = client.post(
        "/api/v1/lyrics/purge",
        json={"source_prefix": "musixmatch", "dry_run": True},
    )
    assert response.status_code == 200
    assert response.json()["rows_matched"] == 2
    assert response.json()["rows_tombstoned"] == 0


def test_all_tracks_listing_carries_lyrics_and_remix_flag(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rb_config, "STATE_DB", db_path)
    client = TestClient(
        create_app(
            backend=SqliteBackend(db_path),
            bind_host="127.0.0.1",
            hostname="test-host",
            state_db_path=str(db_path),
            mount_frontend=False,
        )
    )
    items = client.get("/api/v1/tracks").json()["items"]
    by_id = {track["stable_id"]: track for track in items}
    aaa = by_id["aaa"]
    assert aaa["lyrics"] is not None
    assert aaa["lyrics"]["effective"] == "vocal"
    assert aaa["lyrics"]["n_lines"] == 1
    assert aaa["is_remix"] is False
    assert by_id["bbb"]["lyrics"]["n_lines"] is None
