"""TRYER-10 / TRYER-13: the /api/v1/dj/try routes over a real state.db.

``try_pair`` (the execution layer, a dependency of these routes) is replaced by
a double that returns contract-typed candidates with real WAV files on disk;
the routes, the sqlite store and the etag guard run for real.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.autodj import try_picks
from apps.shared.engine_origin import EngineOrigin
from apps.transition_score import SCORER_VERSION, Candidate, Measured, TryResult, Veto
from apps.transition_score.tryer import TryError
from apps.webui.server.routes import dj_try


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * 2 * 480)
    return path


def _candidate(
    tmp: Path, cid: str, score: float | None, vetoes: tuple[Veto, ...] = (), unknown: dict[str, str] | None = None
) -> Candidate:
    tmp.mkdir(parents=True, exist_ok=True)
    return Candidate(
        id=cid,
        plan={"style": "blend", "bars": 16},
        plan_sha256=f"sha-{cid}",
        style="blend",
        bars=16,
        metrics={"vocal_overlap_s": 0.0},
        factors={"alignment": 0.9},
        vetoes=vetoes,
        measured=Measured(k=5, n=7),
        unknown={"seam_clicks": "no render"} if unknown is None else unknown,
        score=score,
        scorer_version=SCORER_VERSION,
        render_path=str(_wav(tmp / f"{cid}.wav")),
        render_ms=10.0,
        score_ms=2.0,
    )


def _job_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


#: A test may replace the candidates the double returns (default: the three below).
OVERRIDE: dict[str, Any] = {}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, list[dict[str, Any]]]]:
    calls: list[dict[str, Any]] = []
    OVERRIDE.clear()

    def fake_try_pair(
        out_id: str,
        in_id: str,
        *,
        n: int,
        context_bars: int,
        data_dir: Path,
        source: object,
        keep_renders_in: Path,
    ) -> TryResult:
        if "raise" in OVERRIDE:
            if OVERRIDE.get("write_first"):
                _wav(_job_dir(keep_renders_in) / "partial.wav")  # renders already kept when the failure hits
            raise OVERRIDE["raise"]  # type: ignore[misc]
        calls.append(
            {
                "out": out_id,
                "in": in_id,
                "n": n,
                "context_bars": context_bars,
                "data_dir": data_dir,
                "source": source,
                "keep_renders_in": keep_renders_in,
            }
        )
        overridden = tuple(
            # Like the real try_pair, a kept render lives in this job's folder: a pre-built one is copied there.
            dataclasses.replace(c, render_path=str(_wav(_job_dir(keep_renders_in) / f"{c.id}.wav")))
            if c.render_path is not None
            else c
            for c in OVERRIDE.get("candidates", ())
        )
        if OVERRIDE.get("stray"):
            overridden = OVERRIDE["candidates"]  # a defective runner: renders kept outside the job folder
        return TryResult(
            out_id=out_id,
            in_id=in_id,
            candidates=overridden
            or (
                _candidate(keep_renders_in, "A", 0.8),
                _candidate(keep_renders_in, "B", 0.0, (Veto("kick_flam_ms", 18.0, 10.0),)),
                _candidate(keep_renders_in, "C", None),
            ),
        )

    (tmp_path / "state").mkdir()
    app = FastAPI()
    app.state.state_db_path = str(tmp_path / "state" / "state.db")
    app.include_router(dj_try.router, prefix="/api/v1")
    app.dependency_overrides[dj_try.get_try_runner] = lambda: fake_try_pair
    # Explicit, not the tests/conftest.py default, since the origin assertion below depends on it.
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client, calls


def _start(test_client: TestClient) -> dict[str, Any]:
    response = test_client.post("/api/v1/dj/try", json={"out": "trk-out", "in": "trk-in", "n": 3, "context_bars": 4})
    assert response.status_code == 200, response.text
    return response.json()


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
    source = dj_try.ServingTrackSource(tmp_path, origin=origin, state_db=db)

    def no_audio(data_dir: Path, stable_id: str) -> Path:
        raise FileNotFoundError(f"no track {stable_id!r} in {data_dir}/state/state.db")

    monkeypatch.setattr("apps.stems.cli.resolve_audio_path", no_audio)
    with pytest.raises(dj_try.TrackUnavailable, match="ab12") as missing:
        source.audio_path("ab12")
    assert str(tmp_path) not in str(missing.value), "the server path stays in the log"
    source._service = EngineProfiles(origin, transport=httpx.MockTransport(lambda r: httpx.Response(404, text="no")))
    with pytest.raises(dj_try.TrackUnavailable, match="ab12"):
        source.profile("ab12")


@pytest.mark.requirement("TRYER-10")
def test_every_pick_and_undo_error_status_is_declared_in_openapi() -> None:
    """[if] the OpenAPI document is generated [then] pick declares 404 409 412 428 and undo 404 409 412 428, each an ErrorBody, [else stop]."""
    app = FastAPI()
    app.include_router(dj_try.router, prefix="/api/v1")
    paths = app.openapi()["paths"]["/api/v1/dj/try/{job_id}/pick"]
    for method in ("post", "delete"):
        declared = paths[method]["responses"]
        for code in ("404", "409", "412", "428"):
            assert code in declared, (method, code)
            assert declared[code]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorBody")
    document = app.openapi()
    start = document["paths"]["/api/v1/dj/try"]["post"]["responses"]
    shapes = start["422"]["content"]["application/json"]["schema"]["anyOf"]
    names = {shape["$ref"].rsplit("/", 1)[1] for shape in shapes}
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


def _stored_jobs(tmp_path: Path) -> int:
    with sqlite3.connect(tmp_path / "state" / "state.db") as state:
        tables = {row[0] for row in state.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "transition_try_jobs" not in tables:
            return 0
        return int(state.execute("SELECT COUNT(*) FROM transition_try_jobs").fetchone()[0])


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
