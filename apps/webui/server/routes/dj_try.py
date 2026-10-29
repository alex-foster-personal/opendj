"""Transition try-er HTTP surface (TRYER-10..13).

CONTRACT this router owns (mounted under ``/api/v1``):

  POST   /dj/try                                   {out, in, n, context_bars}
         200 TryJobOut {job_id, etag, out, in, n, context_bars, scorer_version,
                        candidates: [CandidateOut], best, best_reason, pick: null}
         ``best`` is ``apps.transition_score.best_candidate``'s id (measured alignment,
         scored, no veto) or null, with ``best_reason`` saying why; stored with the job.
  GET    /dj/try/{job}                             200 TryJobOut (pick filled when one is stored)
  GET    /dj/try/{job}/candidates/{cid}/audio      200 audio/wav, the candidate's render
  POST   /dj/try/{job}/pick   If-Match: <etag>     {candidate} -> 200 TryJobOut
  DELETE /dj/try/{job}/pick   If-Match: <etag>     -> 200 TryJobOut (pick back to null)

Errors: 422 ``try_failed`` (``try_pair`` raised ``TryError``: nothing proposed, a render
failed, the audio extra missing), 404 ``job_not_found`` / ``candidate_not_found`` / ``render_missing``,
409 ``no_pick`` (undo with nothing picked) / ``already_picked`` (undo first; a pick is never replaced), 412 ``etag_mismatch``, 428
``if_match_required``. Pick and undo follow the etag apply/undo pattern of
``routes/play_it.py``: the etag is derived from the job's revision, which every
write bumps, so a client acting on a stale view is refused.

The candidates come from ``apps.transition_score.try_pair`` (the execution
layer); this router stores them, serves their renders, and records the choice.
It computes no score of its own: a candidate's ``score`` is whatever the scorer
returned, including ``null`` when too few factors were measured.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.autodj import try_picks
from apps.autodj.profile import TrackProfile
from apps.shared import file_lock
from apps.shared.engine_origin import EngineOrigin
from apps.transition_score import SCORER_VERSION, Candidate, TryResult, best_candidate, try_pair
from apps.transition_score.tryer import AudioUnavailable, EngineProfiles, EngineTrackSource, TryError

from ..deps import get_library_data_dir, get_library_state_db
from ..errors import ErrorBody

router = APIRouter(prefix="/dj/try", tags=["dj-try"])
_log = logging.getLogger(__name__)

#: The candidate fields the try-er contract ((private path) section 2) promises.
CANDIDATE_FIELDS: tuple[str, ...] = (
    "id",
    "plan",
    "plan_sha256",
    "style",
    "bars",
    "metrics",
    "factors",
    "vetoes",
    "measured",
    "unknown",
    "score",
    "scorer_version",
    "render_path",
    "render_ms",
    "score_ms",
    "rank_group",
    "refused",
)

TryRunner = Callable[..., TryResult]


class TryIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    out_id: str = Field(alias="out", min_length=1)
    in_id: str = Field(alias="in", min_length=1)
    n: int = Field(6, ge=1, le=12)
    context_bars: int = Field(4, ge=1, le=32)


class PickIn(BaseModel):
    candidate: str = Field(min_length=1)


class VetoOut(BaseModel):
    name: str
    value: float
    threshold: float


class MeasuredOut(BaseModel):
    k: int
    n: int


class CandidateOut(BaseModel):
    """One candidate as the try-er answers it: the contract fields minus ``render_path``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    plan: dict[str, Any]
    plan_sha256: str
    style: str
    bars: int
    metrics: dict[str, Any]
    factors: dict[str, float]
    vetoes: list[VetoOut]
    measured: MeasuredOut
    unknown: dict[str, str]
    #: M is 0..1 by contract; a value outside it is a scorer defect and fails validation, never clamped.
    score: float | None = Field(ge=0.0, le=1.0)
    scorer_version: str
    render_ms: float | None
    score_ms: float
    refused: str | None
    rank_group: Literal["measured", "alignment unknown", "refused"]
    render_url: str


class PickOut(BaseModel):
    job_id: str
    out_id: str
    in_id: str
    picked_candidate_id: str
    plan: dict[str, Any]
    plan_sha256: str
    scorer_version: str
    rejected_ids: list[str]
    created_at: str


class TryJobOut(BaseModel):
    """A try job (TRYER-10/13/15): every surface (HTTP, CLI, MCP, panel) reads this shape."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    job_id: str
    etag: str
    out_id: str = Field(alias="out")
    in_id: str = Field(alias="in")
    n: int
    context_bars: int
    scorer_version: str
    created_at: str
    candidates: list[CandidateOut]
    best: str | None
    best_reason: str
    pick: PickOut | None


class TrackUnavailable(TryError):
    """One of the pair's tracks cannot be tried here: no audio file on this server, or no usable analysis."""


@dataclass
class ServingTrackSource(EngineTrackSource):
    """Profiles from THIS server (the socket answering the request), audio from its data dir.

    The default ``EngineTrackSource`` rediscovers an engine from the installed app's lock file,
    which on a worktree or custom-data-dir server is another library or none at all.
    """

    origin: EngineOrigin = field(kw_only=True)
    #: The server's configured library database (nested or flat layout), for audio lookups.
    state_db: Path = field(kw_only=True)

    def __post_init__(self) -> None:
        self._service = EngineProfiles(self.origin)

    def audio_path(self, stable_id: str) -> Path:
        # The shared resolver reads <data_dir>/state/state.db only. A server configured with a
        # flat <root>/<name>.db would otherwise get another library's audio, or none: refuse it.
        nested = self.data_dir / "state" / "state.db"
        if self.state_db.resolve() != nested.resolve():
            # The paths stay in the log: a response never names a server path.
            _log.warning("try-er: flat library layout %s, audio resolves from %s only", self.state_db, nested)
            raise TryError(
                "the try-er resolves audio from the nested <data>/state/state.db layout only, and this server's"
                " library uses the flat layout: not supported yet, see PARITY-TODO"
            )
        try:
            return super().audio_path(stable_id)
        except FileNotFoundError as error:
            _log.warning("try-er: no audio for %s: %s", stable_id, error)  # the server path stays in the log
            raise TrackUnavailable(f"track {stable_id} has no audio on this server: relink or re-import it") from error

    def profile(self, stable_id: str) -> TrackProfile:
        try:
            return super().profile(stable_id)
        except TryError as error:
            raise TrackUnavailable(f"track {stable_id} has no usable analysis here: {error}") from error


def _serving_origin(request: Request, state_db: Path) -> EngineOrigin:
    server = request.scope.get("server")
    if not server or server[1] is None:
        raise RuntimeError(f"the try route cannot name its own socket (scope server={server!r})")
    host, port = server
    return EngineOrigin(host=host, port=int(port), lock_path=state_db, pid=None, role=None, boot_id=None)


#: Each job's kept renders (``try_pair(keep_renders_in=...)``), under the library data dir, so the panel can
#: audition a candidate after the try returns. Jobs have no expiry of their own, so the folder is capped by count.
TRY_JOB_RENDERS = Path("state") / "try-jobs"
#: How many jobs keep their renders: a six-candidate job keeps about 6 x 44 MB (mix plus decks), so about 1 GB
#: in all. Older jobs' audio answers ``render_missing``; their scores, picks and plans stay in state.db.
TRY_JOB_RENDERS_KEPT = 4
#: A prune waits this long for another request's prune (a few rmtree calls), polling every PRUNE_POLL_S.
PRUNE_WAIT_S = 30.0
PRUNE_POLL_S = 0.05


#: Written into a job's render folder once its try has returned: only finished jobs are ever pruned, so a
#: concurrent request cannot delete renders a try is still writing.
JOB_DONE_MARKER = ".done"
#: Held (non-blocking) while one request prunes; a request that finds it held skips pruning this time.
PRUNE_LOCK = ".prune.lock"


def _mark_job_renders_done(folder: Path) -> None:
    (folder / JOB_DONE_MARKER).touch()


@contextmanager
def _render_lock(renders: Path) -> Iterator[None]:
    """One request at a time prunes and publishes job renders. A request that finds the lock held WAITS for it (up to
    ``PRUNE_WAIT_S``, then raises), so no finished job escapes every prune and the cap holds after a burst."""
    renders.mkdir(parents=True, exist_ok=True)
    fd = os.open(renders / PRUNE_LOCK, os.O_RDWR | os.O_CREAT)
    try:
        deadline = time.monotonic() + PRUNE_WAIT_S
        while not file_lock.try_lock(fd):
            if time.monotonic() > deadline:
                raise TimeoutError(f"the try-job render prune lock was held for over {PRUNE_WAIT_S} s")
            time.sleep(PRUNE_POLL_S)
        try:
            yield
        finally:
            file_lock.unlock(fd)
    finally:
        os.close(fd)


def _prune_finished(renders: Path, keep: int) -> None:
    """Delete all but the ``keep`` newest FINISHED job render folders (by their marker's time); hold ``_render_lock``.
    A folder without the finished marker belongs to a try still running, so it is never touched."""
    finished = [p for p in renders.iterdir() if (p / JOB_DONE_MARKER).is_file()]
    finished.sort(key=lambda p: (p / JOB_DONE_MARKER).stat().st_mtime, reverse=True)
    for stale in finished[keep:]:
        shutil.rmtree(stale)


def _prune_job_renders(renders: Path, keep: int) -> None:
    with _render_lock(renders):
        _prune_finished(renders, keep)


def get_try_runner() -> TryRunner:
    """The execution layer's ``try_pair``; route tests override this dependency."""
    return try_pair


# -----------------------------------------------------------------------------
@contextmanager
def _store(state_db: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(state_db)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        try_picks.apply_try_migrations(conn)
        yield conn
    finally:
        conn.close()


def _etag(job: try_picks.TryJob) -> str:
    return f'"try-{job.job_id}-r{job.revision}"'


def _error(code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=code, content=ErrorBody(error=error, message=message).model_dump())


def _candidate_document(candidate: Candidate) -> dict[str, Any]:
    document = candidate.to_json()
    missing = [name for name in CANDIDATE_FIELDS if name not in document]
    if missing:
        raise ValueError(f"try_pair candidate {candidate.id!r} is missing contract fields {missing}")
    return document


def _job_document(request: Request, job: try_picks.TryJob, pick: try_picks.TryPick | None) -> dict[str, Any]:
    candidates = []
    for candidate in job.candidates:
        public = {key: value for key, value in candidate.items() if key != "render_path"}
        # A path, not an absolute URL: behind a proxy the server only knows its own loopback origin,
        # so the client resolves this against its configured API base.
        public["render_url"] = request.app.url_path_for(
            "dj_try_candidate_audio", job_id=job.job_id, candidate_id=candidate["id"]
        )
        candidates.append(public)
    return {
        "job_id": job.job_id,
        "etag": _etag(job),
        "out": job.out_id,
        "in": job.in_id,
        "n": job.n,
        "context_bars": job.context_bars,
        "scorer_version": job.scorer_version,
        "created_at": job.created_at,
        "candidates": candidates,
        "best": job.best_id,
        "best_reason": job.best_reason,
        "pick": None if pick is None else dataclasses.asdict(pick),
    }


def _respond(request: Request, conn: sqlite3.Connection, job_id: str) -> JSONResponse:
    job = try_picks.get_job(conn, job_id)
    validated = TryJobOut.model_validate(_job_document(request, job, try_picks.get_pick(conn, job_id)))
    return JSONResponse(content=validated.model_dump(mode="json", by_alias=True), headers={"ETag": validated.etag})


def _store_error(error: try_picks.TryStoreError) -> JSONResponse:
    codes = {
        "job_not_found": status.HTTP_404_NOT_FOUND,
        "candidate_not_found": status.HTTP_404_NOT_FOUND,
        "no_pick": status.HTTP_409_CONFLICT,
        "already_picked": status.HTTP_409_CONFLICT,
        "stale_revision": status.HTTP_412_PRECONDITION_FAILED,
    }
    return _error(codes[error.code], "etag_mismatch" if error.code == "stale_revision" else error.code, str(error))


def _revision_from_if_match(job: try_picks.TryJob, if_match: str | None) -> int | JSONResponse:
    if if_match is None:
        return _error(428, "if_match_required", f"send If-Match: {_etag(job)} (GET /dj/try/{job.job_id})")
    if if_match.strip() != _etag(job):
        return _error(
            status.HTTP_412_PRECONDITION_FAILED,
            "etag_mismatch",
            f"If-Match {if_match!r} is stale; the job is at {_etag(job)}",
        )
    return job.revision


# -----------------------------------------------------------------------------
_PICK_ERRORS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (404, 409, 412, 428)}


#: A 422 is either the route's own refusal (track_unavailable, try_failed) or FastAPI's request validation.
_START_422: dict[str, Any] = {
    "description": "The pair cannot be tried (ErrorBody), or the request body is malformed (HTTPValidationError)",
    "content": {
        "application/json": {
            "schema": {
                "anyOf": [
                    {"$ref": "#/components/schemas/ErrorBody"},
                    {"$ref": "#/components/schemas/HTTPValidationError"},
                ]
            }
        }
    },
}


@router.post("", response_model=TryJobOut, responses={422: _START_422})
def start_try(
    request: Request,
    body: TryIn,
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
    runner: TryRunner = Depends(get_try_runner),  # noqa: B008  # FastAPI DI
) -> JSONResponse:
    job_id = uuid.uuid4().hex
    renders = data_dir / TRY_JOB_RENDERS
    job_renders = renders / job_id
    try:
        source = ServingTrackSource(data_dir, origin=_serving_origin(request, state_db), state_db=state_db)
        result = runner(
            body.out_id,
            body.in_id,
            n=body.n,
            context_bars=body.context_bars,
            data_dir=data_dir,
            source=source,
            keep_renders_in=job_renders,
        )
    except (TrackUnavailable, AudioUnavailable) as error:
        _discard_job_renders(job_renders)
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "track_unavailable", str(error))
    except TryError as error:
        # The pair cannot be tried (nothing proposed, a render failed, the audio extra missing): say why.
        _discard_job_renders(job_renders)
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "try_failed", str(error))
    except BaseException:
        # An unexpected failure (a full disk while keeping renders): an unfinished folder is never pruned, so it
        # must not outlive the request.
        _discard_job_renders(job_renders)
        raise
    stored = False
    try:
        job, validated = _validated_job(request, body, job_id, result, job_renders)
        # One lock covers publication and pruning, so concurrent tries cannot both publish over the cap. The order is
        # failure-atomic: the new folder is marked, then the job is stored, and only then are older renders pruned.
        # A failure before the insert commits (lock timeout, database error) removes only the new renders and retires
        # nothing; once stored, the job's renders are never removed by this request.
        with _render_lock(renders):
            if job_renders.is_dir():
                _mark_job_renders_done(job_renders)
            with _store(state_db) as conn:
                try_picks.insert_job(conn, job)
            stored = True
            _prune_finished(renders, keep=TRY_JOB_RENDERS_KEPT)
    except BaseException:
        if not stored:
            _discard_job_renders(job_renders)  # nothing was stored, so nothing may point at these renders
        raise
    return JSONResponse(content=validated.model_dump(mode="json", by_alias=True), headers={"ETag": validated.etag})


def _inside(path: str | Path, root: Path) -> bool:
    """Whether ``path`` resolves (symlinks and ``..`` included) to somewhere under ``root``."""
    return Path(path).resolve().is_relative_to(root.resolve())


def _discard_job_renders(job_renders: Path) -> None:
    if job_renders.exists():
        shutil.rmtree(job_renders)


def _validated_job(
    request: Request, body: TryIn, job_id: str, result: TryResult, job_renders: Path
) -> tuple[try_picks.TryJob, TryJobOut]:
    """Build the job and validate its public document BEFORE it is stored, so a bad job never reaches state.db.

    Every kept render must lie inside this job's own render folder: the audio route serves only from there."""
    candidates = [_candidate_document(candidate) for candidate in result.candidates]
    stray = [
        c["id"] for c in candidates if c.get("render_path") is not None and not _inside(c["render_path"], job_renders)
    ]
    if stray:
        raise ValueError(f"try_pair kept renders outside this job's folder for candidates {stray}")
    foreign = sorted({c["scorer_version"] for c in candidates} - {SCORER_VERSION})
    if foreign:
        raise ValueError(f"try_pair returned scorer versions {foreign}, not the running {SCORER_VERSION}")
    best, best_reason = best_candidate(result.candidates)
    job = try_picks.TryJob(
        job_id=job_id,
        out_id=body.out_id,
        in_id=body.in_id,
        n=body.n,
        context_bars=body.context_bars,
        scorer_version=SCORER_VERSION,
        candidates=candidates,
        revision=0,
        created_at=datetime.now(UTC).isoformat(),
        best_id=None if best is None else best.id,
        best_reason=best_reason,
    )
    return job, TryJobOut.model_validate(_job_document(request, job, None))


@router.get("/{job_id}", response_model=TryJobOut, responses={404: {"model": ErrorBody}})
def get_try(
    request: Request,
    job_id: str,
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> JSONResponse:
    with _store(state_db) as conn:
        try:
            return _respond(request, conn, job_id)
        except try_picks.TryStoreError as error:
            return _store_error(error)


@router.get(
    "/{job_id}/candidates/{candidate_id}/audio",
    name="dj_try_candidate_audio",
    response_class=FileResponse,
    response_model=None,
    responses={404: {"model": ErrorBody}},
)
def candidate_audio(
    job_id: str,
    candidate_id: str,
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
) -> FileResponse | JSONResponse:
    with _store(state_db) as conn:
        try:
            job = try_picks.get_job(conn, job_id)
        except try_picks.TryStoreError as error:
            return _store_error(error)
    candidate = next((c for c in job.candidates if c["id"] == candidate_id), None)
    if candidate is None:
        return _error(404, "candidate_not_found", f"try job {job_id!r} has no candidate {candidate_id!r}")
    kept = candidate.get("render_path")
    render = None if kept is None else Path(kept)
    if render is not None and not _inside(render, data_dir / TRY_JOB_RENDERS / job_id):
        # Served only from this job's own folder: a stored path anywhere else is never opened.
        _log.warning("try job %s candidate %s render path %s is outside its job folder", job_id, candidate_id, render)
        return _error(404, "render_missing", f"candidate {candidate_id!r} render is not on disk; render the pair again")
    if render is None or not render.is_file():
        # The server-local path stays in the log; the client only learns which candidate.
        _log.warning("try job %s candidate %s render missing at %s", job_id, candidate_id, render)
        return _error(404, "render_missing", f"candidate {candidate_id!r} render is not on disk; render the pair again")
    return FileResponse(render, media_type="audio/wav")


@router.post("/{job_id}/pick", response_model=TryJobOut, responses=_PICK_ERRORS)
def pick_candidate(
    request: Request,
    job_id: str,
    body: PickIn,
    if_match: str | None = Header(default=None),  # FastAPI DI
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> JSONResponse:
    with _store(state_db) as conn:
        try:
            job = try_picks.get_job(conn, job_id)
            revision = _revision_from_if_match(job, if_match)
            if isinstance(revision, JSONResponse):
                return revision
            try_picks.record_pick(conn, job_id, body.candidate, revision)
        except try_picks.TryStoreError as error:
            return _store_error(error)
        return _respond(request, conn, job_id)


@router.delete("/{job_id}/pick", response_model=TryJobOut, responses=_PICK_ERRORS)
def undo_pick(
    request: Request,
    job_id: str,
    if_match: str | None = Header(default=None),  # FastAPI DI
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> JSONResponse:
    with _store(state_db) as conn:
        try:
            job = try_picks.get_job(conn, job_id)
            revision = _revision_from_if_match(job, if_match)
            if isinstance(revision, JSONResponse):
                return revision
            try_picks.undo_pick(conn, job_id, revision)
        except try_picks.TryStoreError as error:
            return _store_error(error)
        return _respond(request, conn, job_id)
