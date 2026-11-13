"""TRYER-10 / TRYER-13: the /api/v1/dj/try routes over a real state.db.

``try_pair`` (the execution layer, a dependency of these routes) is replaced by
a double that returns contract-typed candidates with real WAV files on disk;
the routes, the sqlite store and the etag guard run for real.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.autodj import try_picks
from apps.autodj.http import build_service
from apps.shared.engine_origin import EngineOrigin
from apps.transition_score.contract import SCORER_VERSION, Candidate, TryResult
from apps.transition_score.tryer import TryError, odj_audio_binary
from apps.webui.server.app_wiring import _install_problem_details_openapi
from apps.webui.server.routes import dj_try
from tests.webui.dj_try_support import (
    LANE_ENGINE,
    LANE_IN,
    LANE_OUT,
    OVERRIDE,
    _candidate,
    _job_dir,
    _lanes_engine,
    _start,
    _stored_jobs,
    _wav,
    dj_try_client,
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, list[dict[str, Any]]]]:
    with dj_try_client(tmp_path) as pair:
        yield pair


@pytest.mark.requirement("TRYER-10")
def test_try_returns_contract_candidates_with_render_urls(client: tuple[TestClient, list[dict[str, Any]]]) -> None:
    """[if] POST /dj/try runs [then] every contract field but render_path comes back, plus a render_url, [else stop]."""
    test_client, calls = client
    job = _start(test_client)
    assert calls[0]["n"] == 3 and calls[0]["out"] == "trk-out"
    assert [c["id"] for c in job["candidates"]] == ["A", "B", "C"]
    first = job["candidates"][0]
    assert set(dj_try.CANDIDATE_FIELDS) - {"render_path"} <= set(first)
    assert "render_path" not in first
    assert first["measured"] == {"k": 5, "n": 7}
    assert job["candidates"][1]["vetoes"] == [{"name": "kick_flam_ms", "value": 18.0, "threshold": 10.0}]
    assert job["candidates"][2]["score"] is None
    assert job["pick"] is None and job["etag"]
    audio = test_client.get(first["render_url"])
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"
    assert audio.content[:4] == b"RIFF"


@pytest.mark.requirement("TRYER-13")
def test_pick_writes_row_and_undo_removes_it(client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path) -> None:
    """[if] a pick carries the current etag [then] the row holds plan, sha, version and rejected ids, and undo deletes it, [else stop]."""
    test_client, _ = client
    job = _start(test_client)
    url = f"/api/v1/dj/try/{job['job_id']}/pick"
    picked = test_client.post(url, json={"candidate": "B"}, headers={"If-Match": job["etag"]})
    assert picked.status_code == 200, picked.text
    pick = picked.json()["pick"]
    assert pick["picked_candidate_id"] == "B" and pick["rejected_ids"] == ["A", "C"]
    assert pick["plan_sha256"] == "sha-B" and pick["scorer_version"] == SCORER_VERSION
    conn = sqlite3.connect(tmp_path / "state" / "state.db")
    assert conn.execute("SELECT picked_candidate_id FROM transition_picks").fetchall() == [("B",)]
    undone = test_client.delete(url, headers={"If-Match": picked.json()["etag"]})
    assert undone.status_code == 200 and undone.json()["pick"] is None
    assert conn.execute("SELECT COUNT(*) FROM transition_picks").fetchone() == (0,)


@pytest.mark.requirement("TRYER-13")
def test_stale_or_missing_etag_is_refused(client: tuple[TestClient, list[dict[str, Any]]]) -> None:
    """[if] a pick sends no If-Match or a stale one [then] 428 / 412 and nothing is stored, [else stop]."""
    test_client, _ = client
    job = _start(test_client)
    url = f"/api/v1/dj/try/{job['job_id']}/pick"
    assert test_client.post(url, json={"candidate": "A"}).status_code == 428
    first = test_client.post(url, json={"candidate": "A"}, headers={"If-Match": job["etag"]})
    assert first.status_code == 200
    stale = test_client.post(url, json={"candidate": "C"}, headers={"If-Match": job["etag"]})
    assert stale.status_code == 412 and stale.json()["error"] == "etag_mismatch"
    assert test_client.get(f"/api/v1/dj/try/{job['job_id']}").json()["pick"]["picked_candidate_id"] == "A"


@pytest.mark.requirement("TRYER-13")
def test_unknown_candidate_and_empty_undo_are_refused(client: tuple[TestClient, list[dict[str, Any]]]) -> None:
    """[if] a pick names a missing candidate or an undo has nothing to undo [then] 404 / 409, [else stop]."""
    test_client, _ = client
    job = _start(test_client)
    url = f"/api/v1/dj/try/{job['job_id']}/pick"
    missing = test_client.post(url, json={"candidate": "Z"}, headers={"If-Match": job["etag"]})
    assert missing.status_code == 404 and missing.json()["error"] == "candidate_not_found"
    empty = test_client.delete(url, headers={"If-Match": job["etag"]})
    assert empty.status_code == 409 and empty.json()["error"] == "no_pick"
    assert test_client.get("/api/v1/dj/try/nope").status_code == 404


@pytest.mark.requirement("TRYER-15")
def test_best_is_the_measured_unvetoed_top_and_null_with_reason_otherwise(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] alignment is unknown on all [then] best is null with a reason, never a fallback, [else stop]."""
    test_client, _ = client
    job = _start(test_client)
    assert job["best"] == "A" and job["best_reason"] == ""
    assert [c["rank_group"] for c in job["candidates"]] == ["measured", "measured", "measured"]
    assert test_client.get(f"/api/v1/dj/try/{job['job_id']}").json()["best"] == "A"
    blind = {"alignment": "too few beats"}
    OVERRIDE["candidates"] = (
        _candidate(tmp_path, "U", 0.99, unknown=blind),
        _candidate(tmp_path, "V", 0.5, unknown=blind),
    )
    unknown = _start(test_client)
    assert unknown["best"] is None
    assert "alignment is unknown" in unknown["best_reason"]
    assert {c["rank_group"] for c in unknown["candidates"]} == {"alignment unknown"}


@pytest.mark.requirement("TRYER-13")
def test_a_second_pick_is_refused_and_undo_then_pick_restores_it(
    client: tuple[TestClient, list[dict[str, Any]]],
) -> None:
    """[if] a job already has a pick [then] another pick is 409 already_picked and the first is kept, [else stop]."""
    test_client, _ = client
    job = _start(test_client)
    url = f"/api/v1/dj/try/{job['job_id']}/pick"
    first = test_client.post(url, json={"candidate": "A"}, headers={"If-Match": job["etag"]}).json()
    again = test_client.post(url, json={"candidate": "C"}, headers={"If-Match": first["etag"]})
    assert again.status_code == 409 and again.json()["error"] == "already_picked"
    undone = test_client.delete(url, headers={"If-Match": first["etag"]}).json()
    restored = test_client.post(url, json={"candidate": "A"}, headers={"If-Match": undone["etag"]}).json()
    assert restored["pick"]["picked_candidate_id"] == "A"


@pytest.mark.requirement("TRYER-10")
def test_missing_render_404_names_no_server_path(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] a render file is gone [then] the 404 says render_missing without the server path, [else stop]."""
    test_client, _ = client
    job = _start(test_client)
    next(tmp_path.rglob("A.wav")).unlink()
    response = test_client.get(job["candidates"][0]["render_url"])
    assert response.status_code == 404 and response.json()["error"] == "render_missing"
    assert str(tmp_path) not in response.text


class _NoRefused(Candidate):
    def to_json(self) -> dict[str, Any]:
        document = super().to_json()
        del document["refused"]
        return document


@pytest.mark.requirement("TRYER-10")
def test_a_candidate_missing_refused_fails_and_stores_nothing(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] try_pair returns a candidate without refused [then] the request fails and no job row is written, [else stop]."""
    test_client, _ = client
    good = _candidate(tmp_path, "A", 0.8)
    OVERRIDE["candidates"] = (_NoRefused(**{f.name: getattr(good, f.name) for f in dataclasses.fields(good)}),)
    with pytest.raises(ValueError, match="refused"):
        test_client.post("/api/v1/dj/try", json={"out": "o", "in": "i", "n": 1, "context_bars": 4})
    conn = sqlite3.connect(tmp_path / "state" / "state.db")
    try_picks.apply_try_migrations(conn)
    assert conn.execute("SELECT COUNT(*) FROM transition_try_jobs").fetchone() == (0,)


@pytest.mark.requirement("TRYER-15")
def test_v1_jobs_get_a_reason_when_migrated_to_v2(tmp_path: Path) -> None:
    """[if] a v1 job row is migrated [then] its null best carries a reason, never an empty one, [else stop]."""
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE transition_try_schema_meta (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    with conn:
        for statement in try_picks._V1:
            conn.execute(statement)
        conn.execute("INSERT INTO transition_try_schema_meta VALUES (1, '2026-10-27T00:00:00+00:00')")
        conn.execute(
            "INSERT INTO transition_try_jobs(job_id, out_id, in_id, n, context_bars, scorer_version,"
            " candidates_json, revision, created_at) VALUES ('j', 'o', 'i', 1, 4, '0.1.0', '[]', 0, 'now')"
        )
    assert try_picks.apply_try_migrations(conn) == 2
    job = try_picks.get_job(conn, "j")
    assert job.best_id is None and job.best_reason.startswith("no best recorded")


@pytest.mark.requirement("TRYER-10")
def test_a_pair_that_cannot_be_tried_is_422_with_the_reason(tmp_path: Path) -> None:
    """[if] try_pair raises TryError [then] the route answers 422 try_failed with its message, not a 500, [else stop]."""

    def failing(*_args: Any, **_kwargs: Any) -> TryResult:
        raise TryError("no valid transition proposed for o -> i (3 considered)")

    (tmp_path / "state").mkdir()
    app = FastAPI()
    app.state.state_db_path = str(tmp_path / "state" / "state.db")
    app.include_router(dj_try.router, prefix="/api/v1")
    app.dependency_overrides[dj_try.get_try_runner] = lambda: failing
    with TestClient(app) as test_client:
        response = test_client.post("/api/v1/dj/try", json={"out": "o", "in": "i", "n": 2, "context_bars": 4})
    assert response.status_code == 422 and response.json()["error"] == "try_failed"
    assert "no valid transition" in response.json()["message"]


@pytest.mark.requirement("TRYER-10")
def test_odj_audio_binary_raises_try_error_when_odj_audio_bin_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] ODJ_AUDIO_BIN points at a missing file [then] odj_audio_binary raises TryError naming ODJ_AUDIO_BIN, [else stop]."""
    missing = tmp_path / "nonexistent-odj-audio"
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(missing))
    with pytest.raises(TryError, match="ODJ_AUDIO_BIN"):
        odj_audio_binary()


@pytest.mark.requirement("TRYER-10")
def test_missing_odj_audio_renderer_is_try_failed_not_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] try_pair needs odj-audio and ODJ_AUDIO_BIN is missing [then] POST answers 422 try_failed naming ODJ_AUDIO_BIN, [else stop]."""
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(tmp_path / "no-such-renderer"))

    def calls_odj_audio_binary(*_args: Any, **_kwargs: Any) -> TryResult:
        odj_audio_binary()
        raise AssertionError("expected TryError from odj_audio_binary")  # pragma: no cover

    (tmp_path / "state").mkdir()
    app = FastAPI()
    app.state.state_db_path = str(tmp_path / "state" / "state.db")
    app.include_router(dj_try.router, prefix="/api/v1")
    app.dependency_overrides[dj_try.get_try_runner] = lambda: calls_odj_audio_binary
    with TestClient(app) as test_client:
        response = test_client.post("/api/v1/dj/try", json={"out": "o", "in": "i", "n": 2, "context_bars": 4})
    assert response.status_code == 422 and response.json()["error"] == "try_failed"
    assert "ODJ_AUDIO_BIN" in response.json()["message"]


@pytest.mark.requirement("TRYER-10")
def test_try_pair_reads_profiles_from_the_serving_app_and_urls_are_paths(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] POST /dj/try runs [then] try_pair gets a source bound to this server's socket and render_url is a path, [else stop]."""
    test_client, calls = client
    job = _start(test_client)
    source = calls[0]["source"]
    assert isinstance(source, dj_try.ServingTrackSource)
    assert (source.origin.host, source.origin.port) == ("127.0.0.1", 80)  # the fixture's TestClient base_url
    assert source.data_dir == calls[0]["data_dir"]
    assert source.state_db == tmp_path / "state" / "state.db"
    assert job["candidates"][0]["render_url"].startswith("/api/v1/dj/try/")


@pytest.mark.requirement("TRYER-10")
def test_a_flat_layout_library_is_refused_not_read_from_another_db(tmp_path: Path) -> None:
    """[if] the server's library is a flat <root>/library.db [then] audio lookup raises TryError, never reads state.db, [else stop]."""
    flat = tmp_path / "library.db"
    source = dj_try.ServingTrackSource(
        tmp_path,
        build_service(),
        origin=EngineOrigin(host="127.0.0.1", port=1, lock_path=flat, pid=None, role=None, boot_id=None),
        state_db=flat,
    )
    with pytest.raises(TryError, match=r"flat layout") as refused:
        source.audio_path("any-id")
    assert str(tmp_path) not in str(refused.value)  # a response never names a server path


@pytest.mark.requirement("TRYER-10")
def test_a_try_keeps_its_renders_under_its_job_folder(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] POST /dj/try runs [then] renders are kept in <data>/state/try-jobs/<job>/ and served from there, [else stop]."""
    test_client, calls = client
    job = _start(test_client)
    kept = calls[0]["keep_renders_in"]
    assert kept == calls[0]["data_dir"] / dj_try.TRY_JOB_RENDERS / job["job_id"]
    assert sorted(p.name for p in kept.iterdir()) == [dj_try.JOB_DONE_MARKER, "A.wav", "B.wav", "C.wav"]
    assert test_client.get(job["candidates"][0]["render_url"]).status_code == 200


@pytest.mark.requirement("TRYER-10")
def test_job_renders_are_capped_by_count_oldest_first(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] more jobs run than TRY_JOB_RENDERS_KEPT [then] only the newest keep renders, the rest 404, [else stop]."""
    import os

    test_client, calls = client
    monkeypatch.setattr(dj_try, "TRY_JOB_RENDERS_KEPT", 2)
    jobs = []
    for index in range(3):
        jobs.append(_start(test_client))
        marker = calls[-1]["keep_renders_in"] / dj_try.JOB_DONE_MARKER
        os.utime(marker, (1_000_000 + index, 1_000_000 + index))  # deterministic order, oldest first
    root = calls[0]["keep_renders_in"].parent
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == sorted(j["job_id"] for j in jobs[1:])
    old = test_client.get(jobs[0]["candidates"][0]["render_url"])
    assert old.status_code == 404 and old.json()["error"] == "render_missing"
    assert test_client.get(f"/api/v1/dj/try/{jobs[0]['job_id']}").status_code == 200


@pytest.mark.requirement("TRYER-10")
def test_pruning_never_touches_an_unfinished_job_and_waits_for_another_prune(tmp_path: Path) -> None:
    """[if] another request holds the prune lock [then] the prune waits, then prunes, never an unfinished job, [else stop]."""
    import os
    import threading

    from apps.shared import file_lock

    renders = tmp_path / "try-jobs"
    for index, name in enumerate(("old-done", "new-done", "running")):
        (renders / name).mkdir(parents=True)
        if name != "running":
            dj_try._mark_job_renders_done(renders / name)
            os.utime(renders / name / dj_try.JOB_DONE_MARKER, (1_000_000 + index, 1_000_000 + index))
    held = os.open(renders / dj_try.PRUNE_LOCK, os.O_RDWR | os.O_CREAT)
    assert file_lock.try_lock(held)
    waiter = threading.Thread(target=dj_try._prune_job_renders, args=(renders, 1))
    try:
        waiter.start()
        waiter.join(timeout=0.5)
        assert waiter.is_alive(), "the prune returned while the lock was held: it skipped instead of waiting"
        assert sorted(p.name for p in renders.iterdir() if p.is_dir()) == ["new-done", "old-done", "running"]
    finally:
        file_lock.unlock(held)
        os.close(held)
    waiter.join(timeout=5)
    assert not waiter.is_alive()
    assert sorted(p.name for p in renders.iterdir() if p.is_dir()) == ["new-done", "running"]


@pytest.mark.requirement("TRYER-10")
def test_a_stale_pick_or_undo_is_412_even_when_the_job_changed_since(tmp_path: Path) -> None:
    """[if] a pick or undo carries a revision another writer moved past [then] stale_revision, never already_picked, [else stop]."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    try_picks.apply_try_migrations(conn)
    job = try_picks.TryJob(
        job_id="j",
        out_id="o",
        in_id="i",
        n=2,
        context_bars=4,
        scorer_version=SCORER_VERSION,
        candidates=[
            {"id": "A", "plan": {}, "plan_sha256": "a", "scorer_version": SCORER_VERSION},
            {"id": "B", "plan": {}, "plan_sha256": "b", "scorer_version": SCORER_VERSION},
        ],
        revision=0,
        created_at="now",
        best_id=None,
        best_reason="none",
    )
    try_picks.insert_job(conn, job)
    try_picks.record_pick(conn, "j", "A", 0)  # another writer: revision 0 -> 1
    with pytest.raises(try_picks.TryStoreError) as stale_pick:
        try_picks.record_pick(conn, "j", "B", 0)
    assert stale_pick.value.code == "stale_revision"
    try_picks.undo_pick(conn, "j", 1)  # revision 1 -> 2, pick gone
    with pytest.raises(try_picks.TryStoreError) as stale_undo:
        try_picks.undo_pick(conn, "j", 1)
    assert stale_undo.value.code == "stale_revision"
    with pytest.raises(try_picks.TryStoreError) as current_empty:
        try_picks.undo_pick(conn, "j", 2)
    assert current_empty.value.code == "no_pick", "control: a current revision still gets the logical refusal"
    assert try_picks.get_job(conn, "j").revision == 2, "a refused write does not bump the revision"


@pytest.mark.requirement("TRYER-10")
def test_an_unavailable_track_is_a_typed_422_not_a_500(client: tuple[TestClient, list[dict[str, Any]]]) -> None:
    """[if] a track has no audio or no usable analysis [then] the try answers 422 track_unavailable naming it, [else stop]."""
    test_client, _ = client
    OVERRIDE["raise"] = dj_try.TrackUnavailable("track trk-in has no audio on this server")
    response = test_client.post("/api/v1/dj/try", json={"out": "trk-out", "in": "trk-in", "n": 2, "context_bars": 4})
    assert response.status_code == 422 and response.json()["error"] == "track_unavailable"
    assert "trk-in" in response.json()["message"]
    # A file gone after resolution, raised inside try_pair, is the same typed 422, never try_failed.
    from apps.transition_score.tryer import AudioUnavailable

    OVERRIDE["raise"] = AudioUnavailable("trk-in")
    gone = test_client.post("/api/v1/dj/try", json={"out": "trk-out", "in": "trk-in", "n": 2, "context_bars": 4})
    assert gone.status_code == 422 and gone.json()["error"] == "track_unavailable"
    assert "trk-in" in gone.json()["message"] and "/" not in gone.json()["message"]


def _engine_answers(response: Any, autodj: object | None = None) -> Any:
    """Return a hook that points the route's ServingTrackSource at a mock engine and reads trk-in's profile."""
    import httpx

    from apps.transition_score.tryer import EngineProfiles

    def read(source: Any) -> None:
        """Swap the source's profile reader for one over the mock engine, then read a profile through it."""
        source._service = EngineProfiles(
            source.origin, autodj or build_service(), transport=httpx.MockTransport(lambda r: response)
        )
        source.profile("trk-in")

    return read


@pytest.mark.requirement("TRYER-9315")
def test_an_engine_failure_reading_a_profile_is_try_failed_not_track_unavailable(
    client: tuple[TestClient, list[dict[str, Any]]],
) -> None:
    """[if] the engine answers 500 to a profile read [then] the try answers 422 try_failed, not track_unavailable, [else stop]."""
    import httpx

    test_client, _ = client
    OVERRIDE["read_profile"] = _engine_answers(httpx.Response(500, text="engine broke"))
    response = test_client.post("/api/v1/dj/try", json={"out": "trk-out", "in": "trk-in", "n": 2, "context_bars": 4})
    assert response.status_code == 422 and response.json()["error"] == "try_failed", response.text
    assert "500" in response.json()["message"]


@pytest.mark.requirement("TRYER-9315")
def test_unusable_analysis_or_a_missing_track_still_reads_track_unavailable(
    client: tuple[TestClient, list[dict[str, Any]]],
) -> None:
    """[if] the analysis payload raises ProfileError or the engine answers 404 [then] the try answers track_unavailable, [else stop]."""
    import httpx

    from apps.autodj.contract import ProfileError

    class _Unusable:
        """An AutoDjService stand-in whose analysis payloads never make a profile."""

        def profile_from_payloads(self, anlz: object, track: object) -> object:
            """Refuse every payload as unusable analysis."""
            raise ProfileError("no beatgrid")

    test_client, _ = client
    body = {"out": "trk-out", "in": "trk-in", "n": 2, "context_bars": 4}
    OVERRIDE["read_profile"] = _engine_answers(httpx.Response(200, json={}), _Unusable())
    unusable = test_client.post("/api/v1/dj/try", json=body)
    assert unusable.status_code == 422 and unusable.json()["error"] == "track_unavailable", unusable.text
    assert "trk-in" in unusable.json()["message"]
    OVERRIDE["read_profile"] = _engine_answers(httpx.Response(404, text="no such track"))
    missing = test_client.post("/api/v1/dj/try", json=body)
    assert missing.status_code == 422 and missing.json()["error"] == "track_unavailable", missing.text


@pytest.mark.requirement("TRYER-10")
def test_the_serving_source_turns_missing_audio_and_analysis_into_track_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the resolver finds no audio, or the engine has no usable analysis [then] ServingTrackSource raises TrackUnavailable, [else stop]."""
    import httpx

    from apps.transition_score.tryer import EngineProfiles

    (tmp_path / "state").mkdir()
    db = tmp_path / "state" / "state.db"
    sqlite3.connect(db).close()
    origin = EngineOrigin(host="127.0.0.1", port=1, lock_path=Path("x.lock"), pid=None, role=None, boot_id=None)
    source = dj_try.ServingTrackSource(tmp_path, build_service(), origin=origin, state_db=db)

    def no_audio(data_dir: Path, stable_id: str) -> Path:
        raise FileNotFoundError(f"no track {stable_id!r} in {data_dir}/state/state.db")

    monkeypatch.setattr("apps.stems.cli.resolve_audio_path", no_audio)
    with pytest.raises(dj_try.TrackUnavailable, match="ab12") as missing:
        source.audio_path("ab12")
    assert str(tmp_path) not in str(missing.value), "the server path stays in the log"
    source._service = EngineProfiles(
        origin, build_service(), transport=httpx.MockTransport(lambda r: httpx.Response(404, text="no"))
    )
    with pytest.raises(dj_try.TrackUnavailable, match="ab12"):
        source.profile("ab12")


@pytest.mark.requirement("TRYER-10")
def test_every_pick_and_undo_error_status_is_declared_in_openapi() -> None:
    """[if] the OpenAPI document is generated [then] pick declares 404 409 412 428 and undo 404 409 412 428, each an ErrorBody, [else stop]."""
    app = FastAPI()
    app.include_router(dj_try.router, prefix="/api/v1")
    _install_problem_details_openapi(app)
    paths = app.openapi()["paths"]["/api/v1/dj/try/{job_id}/pick"]
    for method in ("post", "delete"):
        declared = paths[method]["responses"]
        for code in ("404", "409", "412", "428"):
            assert code in declared, (method, code)
            assert declared[code]["content"]["application/problem+json"]["schema"]["$ref"].endswith("/ErrorBody")
    document = app.openapi()
    start = document["paths"]["/api/v1/dj/try"]["post"]["responses"]
    error_ref = start["422"]["content"]["application/problem+json"]["schema"]["$ref"]
    validation_schema = start["422"]["content"]["application/json"]["schema"]
    if "$ref" in validation_schema:
        validation_names = {validation_schema["$ref"].rsplit("/", 1)[1]}
    else:
        validation_names = {shape["$ref"].rsplit("/", 1)[1] for shape in validation_schema["anyOf"]}
    names = {error_ref.rsplit("/", 1)[1], *validation_names}
    assert names == {"ErrorBody", "HTTPValidationError"}
    assert names <= set(document["components"]["schemas"])  # both refs resolve


@pytest.mark.requirement("TRYER-10")
def test_an_out_of_range_score_fails_validation_never_clamped(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] try_pair returns a score outside 0..1 [then] the response model refuses it, never a clamped value, [else stop]."""
    import pydantic

    test_client, _ = client
    OVERRIDE["candidates"] = (_candidate(tmp_path / "k", "A", 1.5),)
    with pytest.raises(pydantic.ValidationError, match="score"):
        test_client.post("/api/v1/dj/try", json={"out": "o", "in": "i", "n": 1, "context_bars": 4})
    assert _stored_jobs(tmp_path) == 0  # nothing poisoned
    del OVERRIDE["candidates"]
    _start(test_client)  # control: the same probe sees a valid job once stored
    assert _stored_jobs(tmp_path) == 1


@pytest.mark.requirement("TRYER-10")
def test_both_declared_422_shapes_occur_at_runtime(client: tuple[TestClient, list[dict[str, Any]]]) -> None:
    """[if] the body is malformed, or the pair cannot be tried [then] the 422 is HTTPValidationError or ErrorBody, [else stop]."""
    test_client, _ = client
    malformed = test_client.post("/api/v1/dj/try", json={"out": "o", "in": "i", "n": 99, "context_bars": 4})
    assert malformed.status_code == 422 and isinstance(malformed.json()["detail"], list)
    OVERRIDE["raise"] = TryError("nothing proposed")
    refused = test_client.post("/api/v1/dj/try", json={"out": "o", "in": "i", "n": 1, "context_bars": 4})
    assert refused.status_code == 422 and refused.json()["error"] == "try_failed"


@pytest.mark.requirement("TRYER-10")
def test_a_failed_try_never_costs_a_kept_render(
    client: tuple[TestClient, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the cap is full and the next try fails [then] every kept job still has its renders, [else stop]."""
    test_client, calls = client
    monkeypatch.setattr(dj_try, "TRY_JOB_RENDERS_KEPT", 2)
    kept = [_start(test_client)["job_id"] for _ in range(2)]
    root = calls[0]["keep_renders_in"].parent
    OVERRIDE["raise"] = TryError("render failed")
    assert (
        test_client.post("/api/v1/dj/try", json={"out": "o", "in": "i", "n": 1, "context_bars": 4}).status_code == 422
    )
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == sorted(kept)
    del OVERRIDE["raise"]
    newest = _start(test_client)["job_id"]  # control: a stored job does retire the oldest
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == sorted([kept[1], newest])


@pytest.mark.requirement("TRYER-10")
def test_a_render_kept_outside_the_job_folder_is_never_stored(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] try_pair returns a render path outside <data>/state/try-jobs/<job> [then] nothing is stored, [else stop]."""
    test_client, _ = client
    OVERRIDE.update(candidates=(_candidate(tmp_path / "elsewhere", "A", 0.5),), stray=True)
    with pytest.raises(ValueError, match="outside this job's folder"):
        test_client.post("/api/v1/dj/try", json={"out": "o", "in": "i", "n": 1, "context_bars": 4})
    assert _stored_jobs(tmp_path) == 0
    del OVERRIDE["stray"]
    _start(test_client)  # control: the same candidate kept inside its folder is stored
    assert _stored_jobs(tmp_path) == 1


@pytest.mark.requirement("TRYER-10")
def test_the_audio_route_serves_only_from_the_jobs_own_folder(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] a stored render path is outside its job folder [then] the audio route answers 404, [else stop]."""
    test_client, _ = client
    job = _start(test_client)
    url = job["candidates"][0]["render_url"]
    assert test_client.get(url).status_code == 200  # control
    secret = _wav(tmp_path / "secret.wav")
    with sqlite3.connect(tmp_path / "state" / "state.db") as state:
        (raw,) = state.execute(
            "SELECT candidates_json FROM transition_try_jobs WHERE job_id = ?", (job["job_id"],)
        ).fetchone()
        tampered = raw.replace(json.loads(raw)[0]["render_path"], str(secret))
        state.execute("UPDATE transition_try_jobs SET candidates_json = ? WHERE job_id = ?", (tampered, job["job_id"]))
    refused = test_client.get(url)
    assert refused.status_code == 404 and refused.json()["error"] == "render_missing"


@pytest.mark.requirement("TRYER-10")
def test_a_prune_that_times_out_publishes_nothing_and_keeps_every_render(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the prune lock times out [then] nothing is stored and every kept render stays, [else stop]."""
    import os

    from apps.shared import file_lock

    test_client, calls = client
    monkeypatch.setattr(dj_try, "TRY_JOB_RENDERS_KEPT", 1)
    monkeypatch.setattr(dj_try, "PRUNE_WAIT_S", 0.2)
    kept = _start(test_client)["job_id"]
    root = calls[0]["keep_renders_in"].parent
    held = os.open(root / dj_try.PRUNE_LOCK, os.O_RDWR | os.O_CREAT)
    try:
        assert file_lock.try_lock(held)
        with pytest.raises(TimeoutError):
            test_client.post("/api/v1/dj/try", json={"out": "o", "in": "i", "n": 1, "context_bars": 4})
    finally:
        file_lock.unlock(held)
        os.close(held)
    assert _stored_jobs(tmp_path) == 1
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == [kept]


@pytest.mark.requirement("TRYER-10")
def test_a_job_is_published_inside_the_prune_lock(
    client: tuple[TestClient, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a try stores its job [then] the render lock is held across prune, marker and insert, [else stop]."""
    import os

    from apps.shared import file_lock

    test_client, calls = client
    seen: list[bool] = []
    real_insert = try_picks.insert_job

    def probing_insert(conn: sqlite3.Connection, job: try_picks.TryJob) -> None:
        probe = os.open(calls[-1]["keep_renders_in"].parent / dj_try.PRUNE_LOCK, os.O_RDWR | os.O_CREAT)
        try:
            free = file_lock.try_lock(probe)
            if free:
                file_lock.unlock(probe)
            seen.append(not free)
        finally:
            os.close(probe)
        real_insert(conn, job)

    monkeypatch.setattr(try_picks, "insert_job", probing_insert)
    _start(test_client)
    assert seen == [True], "if the insert runs outside the render lock then concurrent tries exceed the cap"


@pytest.mark.requirement("TRYER-10")
def test_an_unexpected_runner_failure_leaves_no_render_folder(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] try_pair raises a non-TryError after keeping renders [then] its job folder is removed, [else stop]."""
    test_client, calls = client
    _start(test_client)  # control: a stored job keeps its folder
    root = calls[0]["keep_renders_in"].parent
    before = sorted(p.name for p in root.iterdir() if p.is_dir())
    OVERRIDE.update({"raise": OSError("no space left on device"), "write_first": True})
    with pytest.raises(OSError, match="no space"):
        test_client.post("/api/v1/dj/try", json={"out": "o", "in": "i", "n": 1, "context_bars": 4})
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == before


@pytest.mark.requirement("TRYER-10")
def test_a_failed_insert_retires_no_kept_render(
    client: tuple[TestClient, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the job cannot be stored [then] its own renders go and every older job keeps its renders, [else stop]."""
    test_client, calls = client
    monkeypatch.setattr(dj_try, "TRY_JOB_RENDERS_KEPT", 1)
    kept = _start(test_client)["job_id"]
    root = calls[0]["keep_renders_in"].parent

    def failing_insert(conn: sqlite3.Connection, job: try_picks.TryJob) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(try_picks, "insert_job", failing_insert)
    with pytest.raises(sqlite3.OperationalError):
        test_client.post("/api/v1/dj/try", json={"out": "o", "in": "i", "n": 1, "context_bars": 4})
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == [kept]


@pytest.mark.requirement("TRYER-16")
def test_lanes_refuse_bad_columns_unknown_candidates_and_missing_renders(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] columns are out of range, the candidate is unknown or its render is gone [then] a typed 4xx, [else stop]."""
    test_client, _ = client
    job = _start(test_client)
    lanes = f"/api/v1/dj/try/{job['job_id']}/candidates"
    bad = test_client.get(f"{lanes}/A/lanes?columns=0")
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_columns"
    unknown = test_client.get(f"{lanes}/Z/lanes")
    assert unknown.status_code == 404 and unknown.json()["error"] == "candidate_not_found"
    next(tmp_path.rglob("A.wav")).unlink()
    gone = test_client.get(f"{lanes}/A/lanes")
    assert gone.status_code == 404 and gone.json()["error"] == "render_missing"
    assert str(tmp_path) not in gone.text, "if the 404 names a server path then it leaks the library location"


# ----- lanes against a real serving engine on loopback (#12514) --------------------------------
@pytest.mark.requirement("PLATFORM-14524")
def test_lanes_answer_a_retryable_engine_unavailable_when_a_profile_read_gets_a_500(tmp_path: Path) -> None:
    """[if] the lanes route's profile read gets an engine 500 [then] it answers 503 engine_unavailable, never track_unavailable, [else stop]."""
    with _lanes_engine(tmp_path) as lanes:
        LANE_ENGINE["profile"] = "engine_500"
        broken = httpx.get(lanes, timeout=30.0)
    assert broken.status_code == 503, broken.text
    assert broken.json()["error"] == "engine_unavailable" and "retry" in broken.json()["message"]
    assert "state.db" not in broken.text, "if the 503 relays the upstream body then it can leak server detail"


@pytest.mark.requirement("PLATFORM-14524")
def test_the_serving_source_turns_an_unreachable_engine_into_engine_unavailable(tmp_path: Path) -> None:
    """[if] the serving engine does not answer a profile read [then] ServingTrackSource raises EngineUnavailable, [else stop]."""
    from apps.transition_score.tryer import EngineProfiles

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    (tmp_path / "state").mkdir()
    db = tmp_path / "state" / "state.db"
    origin = EngineOrigin(host="127.0.0.1", port=1, lock_path=tmp_path / "x.lock", pid=None, role=None, boot_id=None)
    source = dj_try.ServingTrackSource(tmp_path, build_service(), origin=origin, state_db=db)
    source._service = EngineProfiles(origin, build_service(), transport=httpx.MockTransport(refuse))
    with pytest.raises(dj_try.EngineUnavailable, match="ab12") as down:
        source.profile("ab12")
    assert str(tmp_path) not in str(down.value), "if the error names the lock path then a response leaks it"


@pytest.mark.requirement("PLATFORM-14524")
def test_lanes_answer_track_unavailable_when_the_engine_has_no_analysis_for_a_track(tmp_path: Path) -> None:
    """[if] the lanes route's analysis read for a track answers 404 [then] it answers 422 track_unavailable naming it, [else stop]."""
    with _lanes_engine(tmp_path) as lanes:
        LANE_ENGINE["profile"] = "no_analysis"
        missing = httpx.get(lanes, timeout=30.0)
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"] == "track_unavailable" and LANE_IN in missing.json()["message"]


@pytest.mark.requirement("PLATFORM-14524")
def test_lanes_draw_both_decks_when_the_engine_serves_both_profiles(tmp_path: Path) -> None:
    """[if] the lanes route reads both profiles from the serving engine [then] it answers 200 with both decks' lanes, [else stop]."""
    with _lanes_engine(tmp_path) as lanes:
        drawn = httpx.get(lanes, timeout=30.0)
    assert drawn.status_code == 200, drawn.text
    body = drawn.json()
    assert body["candidate"] == "A" and [deck["deck"] for deck in body["decks"]] == [1, 2]
    assert all(len(deck["peaks"]) == 40 for deck in body["decks"]), "if peaks ignore columns then the bound is lost"


@pytest.mark.requirement("PERFORMANCE-SCREEN-14653")
def test_lanes_state_each_profiles_energy_provenance_without_a_relative_comparison(tmp_path: Path) -> None:
    """[if] the engine serves both profiles with waveform energy but no MIK [then] lanes say relative, no step, [else stop]."""
    with _lanes_engine(tmp_path) as lanes:
        drawn = httpx.get(lanes, timeout=30.0)
    assert drawn.status_code == 200, drawn.text
    energy = drawn.json()["energy"]
    assert energy["out"] == {"stable_id": LANE_OUT, "source": "relative", "mik_level": None}
    assert energy["in"] == {"stable_id": LANE_IN, "source": "relative", "mik_level": None}
    assert energy["cross_track_step"] is None, "if relative energy yields a step then the lanes overstate it"
    assert energy["note"].startswith("cross-track energy not compared")


@pytest.mark.requirement("PERFORMANCE-SCREEN-14653")
def test_lanes_for_a_refused_candidate_claim_no_energy_provenance(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] lanes are requested for a refused candidate [then] a 404 render_missing with no energy block, [else stop]."""
    test_client, _ = client
    refused = dataclasses.replace(
        _candidate(tmp_path / "built", "R", None), render_path=None, render_ms=None, refused="render failed: bad plan"
    )
    OVERRIDE["candidates"] = (_candidate(tmp_path / "built", "A", 0.7), refused)
    job = _start(test_client)
    missing = test_client.get(f"/api/v1/dj/try/{job['job_id']}/candidates/R/lanes")
    assert missing.status_code == 404 and missing.json()["error"] == "render_missing"
    assert "energy" not in missing.json(), "if a refused candidate gets energy provenance then a lane is implied"


# ----- a prune that cannot delete an older job's renders (#12502) -------------------------------
@contextmanager
def _undeletable(job_folder: Path) -> Iterator[None]:
    """Gives a stored job's render folder a read-only subfolder holding a file, so a real rmtree of it fails."""
    locked = _job_dir(job_folder / "locked")
    _wav(locked / "stuck.wav")
    locked.chmod(0o500)
    try:
        yield
    finally:
        for moved in job_folder.parent.glob("*/locked"):  # the prune may have renamed the job folder
            moved.chmod(0o700)


def _try_folders(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir())


@pytest.mark.requirement("PLATFORM-14542")
def test_a_prune_that_cannot_delete_an_older_job_still_publishes_and_retires_it_whole(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] pruning an older job's render folder fails [then] the new job answers 200 and the old job's renders all go, [else stop]."""
    import os

    test_client, calls = client
    monkeypatch.setattr(dj_try, "TRY_JOB_RENDERS_KEPT", 1)
    old = _start(test_client)
    root = calls[0]["keep_renders_in"].parent
    os.utime(root / old["job_id"] / dj_try.JOB_DONE_MARKER, (1_000_000, 1_000_000))
    with _undeletable(root / old["job_id"]):
        new = _start(test_client)  # before #12502 the rmtree error escaped as a 500 after the job was stored
        assert _stored_jobs(tmp_path) == 2
        assert test_client.get(new["candidates"][0]["render_url"]).status_code == 200
        for candidate in old["candidates"]:
            gone = test_client.get(candidate["render_url"])
            assert gone.status_code == 404 and gone.json()["error"] == "render_missing", candidate["id"]
        assert _try_folders(root) == sorted([new["job_id"], f"{dj_try.RETIRED_PREFIX}{old['job_id']}"])


@pytest.mark.requirement("PLATFORM-14542")
def test_a_prune_that_succeeds_leaves_no_leftover_and_retries_an_earlier_failed_delete(
    client: tuple[TestClient, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] pruning succeeds after a new job is stored [then] only the newest folders stay, earlier leftovers too go, [else stop]."""
    import os

    test_client, calls = client
    monkeypatch.setattr(dj_try, "TRY_JOB_RENDERS_KEPT", 1)
    first = _start(test_client)["job_id"]
    root = calls[0]["keep_renders_in"].parent
    os.utime(root / first / dj_try.JOB_DONE_MARKER, (1_000_000, 1_000_000))
    second = _start(test_client)["job_id"]
    assert _try_folders(root) == [second], "if a clean prune leaves a retired folder then the cap leaks disk"
    os.utime(root / second / dj_try.JOB_DONE_MARKER, (1_000_001, 1_000_001))
    with _undeletable(root / second):
        third = _start(test_client)["job_id"]
    assert _try_folders(root) == sorted([third, f"{dj_try.RETIRED_PREFIX}{second}"])
    os.utime(root / third / dj_try.JOB_DONE_MARKER, (1_000_002, 1_000_002))
    fourth = _start(test_client)["job_id"]
    assert _try_folders(root) == [fourth], (
        "if the next prune skips the leftover then the failed delete is never retried"
    )


@pytest.mark.requirement("PLATFORM-14542")
def test_recovery_runs_under_the_render_lock_and_hides_the_job_before_deleting(
    client: tuple[TestClient, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a concurrent request probes the render lock during recovery [then] it is held and the old job is already gone, [else stop]."""
    import os

    from apps.shared import file_lock

    test_client, calls = client
    monkeypatch.setattr(dj_try, "TRY_JOB_RENDERS_KEPT", 1)
    old = _start(test_client)["job_id"]
    root = calls[0]["keep_renders_in"].parent
    os.utime(root / old / dj_try.JOB_DONE_MARKER, (1_000_000, 1_000_000))
    seen: list[tuple[str, bool, bool]] = []
    real_rmtree = dj_try.shutil.rmtree

    def probing_rmtree(path: Path, *args: Any, **kwargs: Any) -> None:
        probe = os.open(root / dj_try.PRUNE_LOCK, os.O_RDWR | os.O_CREAT)
        try:
            free = file_lock.try_lock(probe)
            if free:
                file_lock.unlock(probe)
        finally:
            os.close(probe)
        seen.append((Path(path).name, not free, (root / old).exists()))
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(dj_try.shutil, "rmtree", probing_rmtree)
    _start(test_client)
    assert seen == [(f"{dj_try.RETIRED_PREFIX}{old}", True, False)], (
        "if the delete runs outside the lock, or on the job's own path, then a concurrent request sees a half-pruned job"
    )
