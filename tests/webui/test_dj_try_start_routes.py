"""The /api/v1/dj/try start route against a real serving engine and the real ``try_pair``, plus the route's
failure paths: cleanup after a failed try, lanes over corrupt stored plans, malformed candidate metadata,
job-folder confinement, byte-range audio seeking, and the try schemas against the committed OpenAPI document.

Split out of ``test_dj_try_routes.py``; shared helpers live in ``dj_try_support.py``."""

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
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from apps.transition_score.contract import SCORER_VERSION, Candidate, Measured, TryResult
from apps.transition_score.tryer import TryError
from apps.webui.server.routes import dj_try
from tests.autodj.fixtures import anlz_payload, track_row
from tests.waits import start_uvicorn_in_thread, stop_uvicorn_in_thread
from tests.webui.dj_try_support import (
    LANE_BPM,
    LANE_IN,
    LANE_OUT,
    LANE_TRACKS,
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


# ----- the start route against a real serving engine and the real try_pair (#12501) ------------
#: How the engine's track route answers in ``_start_engine``: ``valid``, ``engine_500`` or ``missing`` (404 for LANE_IN).
START_ENGINE: dict[str, str] = {}


@contextmanager
def _start_engine(tmp_path: Path) -> Iterator[str]:
    """The try router with the REAL try_pair plus the profile payloads it reads, on loopback; yields the start URL.

    The library lists both tracks, at audio paths that do not exist on this server."""
    (tmp_path / "state").mkdir()
    db = tmp_path / "state" / "state.db"
    library = sqlite3.connect(db)
    with library:
        library.execute("CREATE TABLE tracks (stable_id TEXT, file_path TEXT, deleted_at TEXT)")
        library.executemany(
            "INSERT INTO tracks VALUES (?, ?, NULL)",
            [(sid, str(tmp_path / "gone" / f"{sid}.flac")) for sid in LANE_TRACKS],
        )
    library.close()
    app = FastAPI()
    app.state.state_db_path = str(db)
    app.include_router(dj_try.router, prefix="/api/v1")
    START_ENGINE["track"] = "valid"

    @app.get("/api/v1/tracks/{stable_id}")
    def track(stable_id: str) -> Any:
        if START_ENGINE["track"] == "engine_500":
            return JSONResponse(status_code=500, content={"error": "internal", "message": "state.db is locked"})
        if START_ENGINE["track"] == "missing" and stable_id == LANE_IN:
            return JSONResponse(status_code=404, content={"error": "track_not_found", "message": "no such track"})
        key, arrangement = LANE_TRACKS[stable_id]
        return track_row(stable_id, bpm=LANE_BPM, key=key, bars=sum(row[1] for row in arrangement))

    @app.get("/api/v1/tracks/{stable_id}/anlz")
    def anlz(stable_id: str) -> Any:
        return anlz_payload(stable_id, bpm=LANE_BPM, arrangement=LANE_TRACKS[stable_id][1])

    engine, thread = start_uvicorn_in_thread(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"), what="try-er start engine"
    )
    try:
        yield f"http://127.0.0.1:{engine.servers[0].sockets[0].getsockname()[1]}/api/v1/dj/try"
    finally:
        stop_uvicorn_in_thread(engine, thread, what="try-er start engine")


def _post_pair(start: str) -> httpx.Response:
    return httpx.post(start, json={"out": LANE_OUT, "in": LANE_IN, "n": 2, "context_bars": 4}, timeout=60.0)


@pytest.mark.requirement("TRYER-10")
def test_start_answers_try_failed_when_the_serving_engine_500s_a_track_profile(tmp_path: Path) -> None:
    """[if] the serving engine answers 500 for a track profile [then] POST /dj/try is 422 try_failed, never track_unavailable, [else stop]."""
    with _start_engine(tmp_path) as start:
        START_ENGINE["track"] = "engine_500"
        broken = _post_pair(start)
    assert broken.status_code == 422, broken.text
    assert broken.json()["error"] == "try_failed", (
        "if a 5xx reads as track_unavailable then a retryable fault blames the library"
    )
    assert "engine fault" in broken.json()["message"] and "retry" in broken.json()["message"]
    assert "state.db" not in broken.text, "if the 422 relays the upstream body then it can leak server detail"
    assert _stored_jobs(tmp_path) == 0


@pytest.mark.requirement("TRYER-10")
def test_start_answers_track_unavailable_when_the_serving_engine_404s_a_track_profile(tmp_path: Path) -> None:
    """[if] the serving engine answers 404 for a track profile [then] POST /dj/try is 422 track_unavailable naming it, [else stop]."""
    with _start_engine(tmp_path) as start:
        START_ENGINE["track"] = "missing"
        missing = _post_pair(start)
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"] == "track_unavailable" and LANE_IN in missing.json()["message"]
    assert _stored_jobs(tmp_path) == 0


@pytest.mark.requirement("TRYER-10")
def test_start_answers_track_unavailable_when_the_track_audio_is_absent_on_the_server(tmp_path: Path) -> None:
    """[if] both profiles read but the track audio is absent on the serving server [then] 422 track_unavailable, no path, [else stop]."""
    with _start_engine(tmp_path) as start:
        absent = _post_pair(start)
    assert absent.status_code == 422, absent.text
    assert absent.json()["error"] == "track_unavailable" and LANE_OUT in absent.json()["message"]
    assert "no audio" in absent.json()["message"], (
        "if this reads as missing analysis then the audio branch went untested"
    )
    assert str(tmp_path) not in absent.text, "if the 422 names the missing file then it leaks the library location"
    assert not (tmp_path / dj_try.TRY_JOB_RENDERS).exists() or not any((tmp_path / dj_try.TRY_JOB_RENDERS).iterdir())


# ----- cleanup after a failed try never hides the failure (#12507) ------------------------------
def _try_app(tmp_path: Path, runner: Any) -> TestClient:
    """A TestClient over the real try router with ``runner`` as try_pair and an empty library in ``tmp_path``."""
    (tmp_path / "state").mkdir()
    app = FastAPI()
    app.state.state_db_path = str(tmp_path / "state" / "state.db")
    app.include_router(dj_try.router, prefix="/api/v1")
    app.dependency_overrides[dj_try.get_try_runner] = lambda: runner
    return TestClient(app, base_url="http://127.0.0.1")


def _post_once(test_client: TestClient) -> httpx.Response:
    return test_client.post("/api/v1/dj/try", json={"out": "o", "in": "i", "n": 1, "context_bars": 4})


def _renders_root(tmp_path: Path) -> Path:
    return tmp_path / dj_try.TRY_JOB_RENDERS


def _fails_after_a_partial_render(*_args: Any, keep_renders_in: Path, **_kwargs: Any) -> TryResult:
    _wav(_job_dir(keep_renders_in / "A") / "mix.wav")  # one candidate kept, the next one's render fails
    raise TryError("odj-audio render failed (3): bad plan")


@pytest.mark.requirement("PLATFORM-14564")
def test_a_runner_failure_after_a_partial_folder_answers_its_refusal_and_leaves_nothing(tmp_path: Path) -> None:
    """[if] a render runner fails after creating a partial folder [then] 422 try_failed with its reason, no folder, no job, [else stop]."""
    with _try_app(tmp_path, _fails_after_a_partial_render) as test_client:
        refused = _post_once(test_client)
    assert refused.status_code == 422 and refused.json()["error"] == "try_failed"
    assert "bad plan" in refused.json()["message"]
    assert [p.name for p in _renders_root(tmp_path).iterdir() if p.is_dir()] == []
    assert _stored_jobs(tmp_path) == 0


def _keeps_an_out_of_range_score(*_args: Any, keep_renders_in: Path, **_kwargs: Any) -> TryResult:
    return TryResult(out_id="o", in_id="i", candidates=(_candidate(keep_renders_in, "A", 1.5),))


@pytest.mark.requirement("PLATFORM-14564")
def test_a_job_that_fails_validation_after_its_renders_are_kept_is_never_stored(tmp_path: Path) -> None:
    """[if] job validation fails after renders are kept [then] the validation error surfaces, renders go, no job, [else stop]."""
    import pydantic

    with (
        _try_app(tmp_path, _keeps_an_out_of_range_score) as test_client,
        pytest.raises(pydantic.ValidationError, match="score"),
    ):
        _post_once(test_client)
    assert [p.name for p in _renders_root(tmp_path).iterdir() if p.is_dir()] == []
    assert _stored_jobs(tmp_path) == 0


def _with_an_undeletable_folder(runner: Any) -> Any:
    """Wraps a runner so its job folder first gets a read-only subfolder holding a file: a real rmtree of it fails."""

    def wrapped(*args: Any, keep_renders_in: Path, **kwargs: Any) -> TryResult:
        locked = _job_dir(keep_renders_in / "locked")
        _wav(locked / "stuck.wav")
        locked.chmod(0o500)
        return runner(*args, keep_renders_in=keep_renders_in, **kwargs)

    return wrapped


@pytest.mark.requirement("PLATFORM-14564")
@pytest.mark.parametrize("runner", [_fails_after_a_partial_render, _keeps_an_out_of_range_score])
def test_a_cleanup_that_raises_never_replaces_the_original_failure(tmp_path: Path, runner: Any) -> None:
    """[if] cleanup itself raises while handling a runner or validation failure [then] the original error is the answer, [else stop]."""
    import pydantic

    try:
        with _try_app(tmp_path, _with_an_undeletable_folder(runner)) as test_client:
            if runner is _fails_after_a_partial_render:
                refused = _post_once(test_client)  # before #12507 the rmtree PermissionError escaped as a 500
                assert refused.status_code == 422 and refused.json()["error"] == "try_failed"
                assert "bad plan" in refused.json()["message"]
            elif runner is _keeps_an_out_of_range_score:
                with pytest.raises(pydantic.ValidationError, match="score"):
                    _post_once(test_client)
        assert _stored_jobs(tmp_path) == 0
        (leftover,) = [p.name for p in _renders_root(tmp_path).iterdir() if p.is_dir()]
        assert leftover.startswith(dj_try.RETIRED_PREFIX), (
            "if the failed folder keeps its job name then no prune retries it"
        )
    finally:
        for locked in _renders_root(tmp_path).glob("*/locked"):
            locked.chmod(0o700)


# ----- lanes over corrupt persisted plan data (#12506) ------------------------------------------
def _rewrite_stored_plan(tmp_path: Path, rewrite: Any) -> None:
    """Rewrites candidate A's stored plan in the one job ``_lanes_engine`` stored, as a corrupt or old row would."""
    with sqlite3.connect(tmp_path / "state" / "state.db") as state:
        ((job_id, raw),) = state.execute("SELECT job_id, candidates_json FROM transition_try_jobs").fetchall()
        candidates = json.loads(raw)
        candidates[0]["plan"] = rewrite(candidates[0]["plan"])
        state.execute(
            "UPDATE transition_try_jobs SET candidates_json = ? WHERE job_id = ?", (json.dumps(candidates), job_id)
        )


@pytest.mark.requirement("PLATFORM-14578")
@pytest.mark.parametrize(
    "rewrite",
    [lambda _plan: '{"style": "crossfade", "events": [', lambda plan: {**plan, "events": []}],
    ids=["plan-is-a-broken-json-string", "plan-without-events"],
)
def test_lanes_answer_a_typed_422_for_a_malformed_stored_plan(tmp_path: Path, rewrite: Any) -> None:
    """[if] a stored candidate contains malformed plan JSON [then] lanes answer 422 lanes_failed naming it, never a 500, [else stop]."""
    with _lanes_engine(tmp_path) as lanes:
        _rewrite_stored_plan(tmp_path, rewrite)
        corrupt = httpx.get(lanes, timeout=30.0)
    assert corrupt.status_code == 422, corrupt.text
    assert corrupt.json()["error"] == "lanes_failed" and "candidate 'A'" in corrupt.json()["message"]
    assert "Traceback" not in corrupt.text and str(tmp_path) not in corrupt.text


def _with_a_crossfader_move(plan: dict[str, Any]) -> dict[str, Any]:
    return {**plan, "events": [*plan["events"], {"at_bar": 1.0, "set": {"crossfader": 0.5}}]}


@pytest.mark.requirement("PLATFORM-14578")
def test_lanes_answer_a_typed_422_for_a_stored_plan_with_an_unsupported_render_control(tmp_path: Path) -> None:
    """[if] a stored candidate's plan moves a control the render cannot apply [then] lanes answer 422 lanes_failed, [else stop]."""
    with _lanes_engine(tmp_path) as lanes:
        _rewrite_stored_plan(tmp_path, _with_a_crossfader_move)
        unsupported = httpx.get(lanes, timeout=30.0)
    assert unsupported.status_code == 422, unsupported.text
    assert unsupported.json()["error"] == "lanes_failed" and "crossfader" in unsupported.json()["message"]


@pytest.mark.requirement("PLATFORM-14578")
def test_lanes_draw_a_stored_plan_that_round_trips_through_the_store(tmp_path: Path) -> None:
    """[if] a stored candidate contains a valid plan, rewritten as it was stored [then] lanes answer 200 with both decks, [else stop]."""
    with _lanes_engine(tmp_path) as lanes:
        _rewrite_stored_plan(tmp_path, lambda plan: json.loads(json.dumps(plan)))
        drawn = httpx.get(lanes, timeout=30.0)
    assert drawn.status_code == 200, drawn.text
    assert [deck["deck"] for deck in drawn.json()["decks"]] == [1, 2]


# ----- malformed candidate metadata never reaches the job store (#12515) -------------------------
def _without_field(name: str) -> type[Candidate]:
    """A Candidate whose contract document omits ``name``, as a regressed execution layer would answer it."""

    class Omitting(Candidate):
        def to_json(self) -> dict[str, Any]:
            document = super().to_json()
            del document[name]
            return document

    return Omitting


def _returning(*candidates: Any) -> Any:
    """A try_pair stand-in that keeps each candidate's render in this job's folder, as the real one does."""

    def runner(*_args: Any, keep_renders_in: Path, **_kwargs: Any) -> TryResult:
        kept = tuple(
            dataclasses.replace(c, render_path=str(_wav(_job_dir(keep_renders_in) / f"{c.id}.wav"))) for c in candidates
        )
        return TryResult(out_id="o", in_id="i", candidates=kept)

    return runner


@pytest.mark.requirement("TRYER-10")
@pytest.mark.parametrize("name", dj_try.CANDIDATE_FIELDS)
def test_a_candidate_omitting_any_contract_field_is_refused_before_storage(tmp_path: Path, name: str) -> None:
    """[if] a candidate omits a required contract field [then] the try fails naming it, no job and no renders, [else stop]."""
    good = _candidate(tmp_path / "built", "A", 0.5)
    omitting = _without_field(name)(**{f.name: getattr(good, f.name) for f in dataclasses.fields(good)})
    with _try_app(tmp_path, _returning(omitting)) as test_client, pytest.raises(ValueError, match=f"'{name}'"):
        _post_once(test_client)
    assert _stored_jobs(tmp_path) == 0, "if a partial document is stored then every reader of the job can crash"
    assert [p.name for p in _renders_root(tmp_path).iterdir() if p.is_dir()] == [], "the refused job's renders go too"


@pytest.mark.requirement("TRYER-10")
@pytest.mark.parametrize("version", ["0.10.0", "", f"{SCORER_VERSION} "], ids=["older", "empty", "padded"])
def test_a_candidate_reporting_a_foreign_scorer_version_is_refused_before_storage(tmp_path: Path, version: str) -> None:
    """[if] one candidate reports a foreign scorer version [then] the whole try fails naming it and stores nothing, [else stop]."""
    current = _candidate(tmp_path / "built", "A", 0.5)
    foreign = dataclasses.replace(_candidate(tmp_path / "built", "B", 0.6), scorer_version=version)
    with _try_app(tmp_path, _returning(current, foreign)) as test_client, pytest.raises(ValueError, match="versions"):
        _post_once(test_client)
    assert _stored_jobs(tmp_path) == 0, "if a mixed-version job is stored then its scores are not comparable"
    same = dataclasses.replace(foreign, scorer_version=SCORER_VERSION)
    with _try_app(_job_dir(tmp_path / "control"), _returning(current, same)) as control:
        assert _post_once(control).status_code == 200  # control: the running version on both is stored
    assert _stored_jobs(tmp_path / "control") == 1


@pytest.mark.requirement("TRYER-10")
@pytest.mark.parametrize(
    ("score", "accepted"),
    [(-0.01, False), (0.0, True), (1.0, True), (1.0000001, False), (float("nan"), False), (float("-inf"), False)],
    ids=["below-zero", "zero", "one", "above-one", "nan", "minus-infinity"],
)
def test_a_score_outside_zero_through_one_is_refused_before_storage(
    tmp_path: Path, score: float, accepted: bool
) -> None:
    """[if] a candidate's score lies outside 0..1 (or is not a number) [then] the try fails and stores nothing, [else stop]."""
    import pydantic

    with _try_app(tmp_path, _returning(_candidate(tmp_path / "built", "A", score))) as test_client:
        if accepted:
            stored = _post_once(test_client)
            assert stored.status_code == 200 and stored.json()["candidates"][0]["score"] == score
        elif not accepted:
            with pytest.raises(pydantic.ValidationError, match="score"):
                _post_once(test_client)
    assert _stored_jobs(tmp_path) == (1 if accepted else 0), (
        "if an out-of-range score is stored then readers rank on it"
    )


# ----- lanes and audio never cross a try job's folder (#12508) -----------------------------------
@contextmanager
def _two_lane_jobs(tmp_path: Path) -> Iterator[tuple[str, str, str]]:
    """Two stored jobs on the real lanes engine; yields (try URL, first job id, second job id)."""
    with _lanes_engine(tmp_path) as lanes:
        start, first = lanes.split("/candidates/", 1)[0].rsplit("/", 1)
        second = httpx.post(start, json={"out": LANE_OUT, "in": LANE_IN, "n": 1}, timeout=30.0)
        assert second.status_code == 200, second.text
        yield start, first, second.json()["job_id"]


def _point_stored_render(tmp_path: Path, job_id: str, render: Path | str) -> None:
    """Rewrites candidate A's stored render path in ``job_id``, as a corrupt or tampered row would hold it."""
    with sqlite3.connect(tmp_path / "state" / "state.db") as state:
        (raw,) = state.execute("SELECT candidates_json FROM transition_try_jobs WHERE job_id = ?", (job_id,)).fetchone()
        candidates = json.loads(raw)
        candidates[0]["render_path"] = str(render)
        state.execute(
            "UPDATE transition_try_jobs SET candidates_json = ? WHERE job_id = ?", (json.dumps(candidates), job_id)
        )


def _lanes_and_audio(start: str, job_id: str) -> tuple[httpx.Response, httpx.Response]:
    candidate = f"{start}/{job_id}/candidates/A"
    return httpx.get(f"{candidate}/lanes?columns=40", timeout=30.0), httpx.get(f"{candidate}/audio", timeout=30.0)


def _assert_render_missing(*responses: httpx.Response) -> None:
    for response in responses:
        assert response.status_code == 404, response.text
        assert response.json()["error"] == "render_missing", "if a foreign render is drawn or played then jobs leak"


@pytest.mark.requirement("TRYER-16")
@pytest.mark.parametrize("form", ["direct", "traversal"])
def test_a_candidate_pointing_at_another_jobs_kept_render_is_refused(tmp_path: Path, form: str) -> None:
    """[if] a stored candidate points at another job's kept render [then] lanes and audio answer render_missing, [else stop]."""
    with _two_lane_jobs(tmp_path) as (start, first, second):
        root = tmp_path / dj_try.TRY_JOB_RENDERS
        foreign = root / first / "A" / "A.wav"
        assert foreign.is_file(), "the other job's render must exist, or this proves only a missing file"
        _point_stored_render(
            tmp_path, second, foreign if form == "direct" else root / second / ".." / first / "A" / "A.wav"
        )
        _assert_render_missing(*_lanes_and_audio(start, second))
        assert [r.status_code for r in _lanes_and_audio(start, first)] == [200, 200]  # control: the owner still draws


@pytest.mark.requirement("TRYER-16")
@pytest.mark.parametrize("link", ["file", "folder", "outside"])
def test_a_candidate_path_escaping_through_a_symlink_is_refused(tmp_path: Path, link: str) -> None:
    """[if] a stored render path lies in its own job folder but a symlink leads out [then] lanes and audio 404, [else stop]."""
    with _two_lane_jobs(tmp_path) as (start, first, second):
        root = tmp_path / dj_try.TRY_JOB_RENDERS
        own = root / second / "A"
        if link == "file":
            escape = own / "escape.wav"
            escape.symlink_to(root / first / "A" / "A.wav")
        elif link == "folder":
            (own / "linked").symlink_to(root / first / "A", target_is_directory=True)
            escape = own / "linked" / "A.wav"
        elif link == "outside":
            escape = own / "secret.wav"
            escape.symlink_to(_wav(tmp_path / "secret.wav"))
        assert escape.resolve().is_file() and not escape.resolve().is_relative_to((root / second).resolve())
        _point_stored_render(tmp_path, second, escape)
        _assert_render_missing(*_lanes_and_audio(start, second))


@pytest.mark.requirement("TRYER-16")
def test_a_candidate_pointing_at_its_own_kept_render_draws_and_plays(tmp_path: Path) -> None:
    """[if] each stored candidate points at its own job's kept render [then] lanes and audio answer 200 from it, [else stop]."""
    with _two_lane_jobs(tmp_path) as (start, first, second):
        for job_id in (first, second):
            lanes, audio = _lanes_and_audio(start, job_id)
            assert lanes.status_code == 200, lanes.text
            assert [deck["deck"] for deck in lanes.json()["decks"]] == [1, 2]
            own = tmp_path / dj_try.TRY_JOB_RENDERS / job_id / "A" / "A.wav"
            assert audio.status_code == 200 and audio.content == own.read_bytes(), "the job's own render is served"


# ----- candidate audio seeking: byte-range requests on a kept WAV (#12505) -----------------------
def _kept_audio(tmp_path: Path, job: dict[str, Any]) -> bytes:
    return (tmp_path / dj_try.TRY_JOB_RENDERS / job["job_id"] / "A.wav").read_bytes()


@pytest.mark.requirement("TRYER-10")
@pytest.mark.parametrize("first, last", [(0, 99), (44, 1043), (100, None)], ids=["head", "seek", "open-ended"])
def test_a_valid_byte_range_on_a_kept_wav_answers_206_with_those_bytes(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path, first: int, last: int | None
) -> None:
    """[if] a browser requests a valid byte range of a kept WAV [then] 206 with exactly those bytes and their range, [else stop]."""
    test_client, _ = client
    job = _start(test_client)
    url, kept = job["candidates"][0]["render_url"], _kept_audio(tmp_path, job)
    whole = test_client.get(url)
    assert whole.status_code == 200 and whole.headers["accept-ranges"] == "bytes", (
        "if ranges are not advertised then seeks re-download"
    )
    end = len(kept) - 1 if last is None else last
    ranged = test_client.get(url, headers={"Range": f"bytes={first}-{'' if last is None else last}"})
    assert ranged.status_code == 206, ranged.text
    assert ranged.headers["content-range"] == f"bytes {first}-{end}/{len(kept)}"
    assert ranged.headers["content-type"] == "audio/wav" and int(ranged.headers["content-length"]) == end - first + 1
    assert ranged.content == kept[first : end + 1], "if the slice is wrong then a seek plays the wrong audio"


@pytest.mark.requirement("TRYER-10")
@pytest.mark.parametrize("suffix", [100, 1])
def test_a_suffix_byte_range_on_a_kept_wav_answers_its_last_bytes(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path, suffix: int
) -> None:
    """[if] a browser requests a suffix range (bytes=-N) of a kept WAV [then] 206 with its last N bytes, [else stop]."""
    test_client, _ = client
    job = _start(test_client)
    kept = _kept_audio(tmp_path, job)
    tail = test_client.get(job["candidates"][0]["render_url"], headers={"Range": f"bytes=-{suffix}"})
    assert tail.status_code == 206, tail.text
    assert tail.headers["content-range"] == f"bytes {len(kept) - suffix}-{len(kept) - 1}/{len(kept)}"
    assert tail.content == kept[-suffix:], "if a suffix range reads from the start then end-of-clip seeks break"


@pytest.mark.requirement("TRYER-10")
def test_a_byte_range_after_the_render_is_pruned_answers_render_missing(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a browser requests a byte range of a render the cap pruned [then] 404 render_missing, no bytes, no path, [else stop]."""
    import os

    test_client, calls = client
    monkeypatch.setattr(dj_try, "TRY_JOB_RENDERS_KEPT", 1)
    old = _start(test_client)
    url = old["candidates"][0]["render_url"]
    assert test_client.get(url, headers={"Range": "bytes=0-99"}).status_code == 206  # control: seekable while kept
    os.utime(calls[0]["keep_renders_in"] / dj_try.JOB_DONE_MARKER, (1_000_000, 1_000_000))
    _start(test_client)  # a newer job: the real prune retires the old job's renders
    assert not calls[0]["keep_renders_in"].exists()
    for ranges in ("bytes=0-99", "bytes=-100"):
        pruned = test_client.get(url, headers={"Range": ranges})
        assert pruned.status_code == 404 and pruned.json()["error"] == "render_missing", ranges
        assert "content-range" not in pruned.headers and str(tmp_path) not in pruned.text


# ----- the route's try schemas and the committed client schema agree (#12513) --------------------
#: The try schemas the panel's generated client types come from (``pnpm run api:gen`` over ``apps/webui/openapi.json``).
TRY_SCHEMAS = (
    "TryJobOut",
    "CandidateOut",
    "PickOut",
    "MeasuredOut",
    "VetoOut",
    "TryLanesOut",
    "DeckLaneOut",
    "LaneEnergyOut",
    "ProfileEnergyOut",
)
COMMITTED_OPENAPI = Path(__file__).resolve().parents[2] / "apps" / "webui" / "openapi.json"


def _route_schemas() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(dj_try.router, prefix="/api/v1")
    return app.openapi()["components"]["schemas"]


def _nullable(prop: dict[str, Any]) -> bool:
    return any(branch.get("type") == "null" for branch in prop.get("anyOf", []))


@pytest.mark.requirement("TRYER-10")
def test_a_null_score_for_insufficient_measurements_is_declared_and_answered_as_null(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] a candidate's score is null for too few measured factors [then] schema and answer both carry null, [else stop]."""
    candidate = _route_schemas()["CandidateOut"]
    score = candidate["properties"]["score"]
    assert "score" in candidate["required"] and _nullable(score), "if score is optional then the panel reads 0"
    (bounded,) = [branch for branch in score["anyOf"] if branch.get("type") == "number"]
    assert (bounded["minimum"], bounded["maximum"]) == (0.0, 1.0)
    test_client, _ = client
    sparse = dataclasses.replace(
        _candidate(tmp_path / "built", "N", None, unknown={"alignment": "too few beats"}), measured=Measured(k=2, n=7)
    )
    OVERRIDE["candidates"] = (sparse,)
    job = _start(test_client)
    for answered in (job, test_client.get(f"/api/v1/dj/try/{job['job_id']}").json()):
        (only,) = answered["candidates"]
        assert "score" in only and only["score"] is None and only["measured"] == {"k": 2, "n": 7}
        assert answered["best"] is None and answered["best_reason"], "an unscored candidate is never best"


@pytest.mark.requirement("TRYER-10")
def test_a_candidate_with_no_kept_render_declares_and_answers_a_render_url_that_404s(
    client: tuple[TestClient, list[dict[str, Any]]], tmp_path: Path
) -> None:
    """[if] a candidate has no kept render path [then] it still answers a render_url, a null render_ms, and 404s, [else stop]."""
    candidate = _route_schemas()["CandidateOut"]
    assert "render_path" not in candidate["properties"], "if render_path is public then a server path leaks"
    url = candidate["properties"]["render_url"]
    assert "render_url" in candidate["required"] and url["type"] == "string" and not _nullable(url)
    assert {"render_ms", "refused"} <= set(candidate["required"])
    assert _nullable(candidate["properties"]["render_ms"]) and _nullable(candidate["properties"]["refused"])
    test_client, _ = client
    refused = dataclasses.replace(
        _candidate(tmp_path / "built", "R", None), render_path=None, render_ms=None, refused="render failed: bad plan"
    )
    OVERRIDE["candidates"] = (_candidate(tmp_path / "built", "A", 0.7), refused)
    job = _start(test_client)
    unrendered = job["candidates"][1]
    assert "render_path" not in unrendered and unrendered["render_url"].endswith("/candidates/R/audio")
    assert unrendered["render_ms"] is None and unrendered["refused"] == "render failed: bad plan"
    for path in (unrendered["render_url"], f"/api/v1/dj/try/{job['job_id']}/candidates/R/lanes"):
        missing = test_client.get(path)
        assert missing.status_code == 404 and missing.json()["error"] == "render_missing", path


@pytest.mark.requirement("TRYER-10")
def test_the_route_try_schemas_match_the_committed_openapi_document() -> None:
    """[if] the route adds, removes or retypes a try field [then] it differs from the committed openapi.json, [else stop]."""
    committed = json.loads(COMMITTED_OPENAPI.read_text(encoding="utf-8"))["components"]["schemas"]
    route = _route_schemas()
    for name in TRY_SCHEMAS:
        assert route[name] == committed[name], (
            f"if {name} drifts from apps/webui/openapi.json then the panel's generated types lie: run"
            " `MDT_LIBRARY_MODE=local just openapi-dump` and `pnpm run api:gen`"
        )
