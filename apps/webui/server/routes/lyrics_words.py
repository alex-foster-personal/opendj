"""Karaoke word-level lyric endpoints: verdicts, word timings, and overrides.

Agent-native parity (house requirement): every action the lyrics UI offers has
an endpoint here, so the whole flow is drivable headlessly and testable without
a browser. The application integrator mounts :data:`router` at ``/api/v1``.

Reads use :func:`routes.cloudsync.get_cloudsync_conn`; writes use
:func:`routes.cloudsync.get_cloudsync_write_conn`. Word bytes come from
:mod:`apps.lyrics.artifacts`, never from a ``lyric_word`` table.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.lyrics import artifacts, jobs as lyric_jobs, lines as lyric_lines, purge, sources_config, store
from apps.lyrics.vocal_presence import NO_LYRICS_MAX_COVERAGE, SPARSE_MAX_COVERAGE
from apps.webui.server.backend import StateBackend
from apps.webui.server.deps import get_read_state

from .cloudsync import get_cloudsync_conn, get_cloudsync_write_conn

router = APIRouter(tags=["lyrics"])


class KaraokeWordOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    idx: int
    word: str
    start_s: float | None = None
    end_s: float | None = None
    score: float | None = None
    witness: str | None = Field(
        default=None,
        description="ASR-witness verdict: agree/drift/unheard/unmatchable are "
                    "shown normally; contradict/lost are the suspect classes.",
    )
    line_final: bool = False
    asr_delta_s: float | None = None


class CoverageVerdictOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    verdict: str = Field(description="computed verdict from stem vocal coverage")
    effective: str = Field(description="what to act on: override if set, else verdict")
    coverage_pct: float | None = None
    source: str | None = Field(default=None, description="lyric provider + match method")
    language_iso3: str | None = None
    n_words: int | None = None
    n_lines: int | None = None
    has_words: bool = Field(
        description="true when words_content_hash is set on the verdict row"
    )
    pct_witness_red: float | None = Field(
        default=None, description="share of words the independent ASR witness distrusts"
    )
    override: str | None = None
    override_note: str | None = None
    pipeline_version: str | None = None
    words_content_hash: str | None = None
    computed_at: str
    updated_at: str
    title: str | None = None
    artist: str | None = None


class KaraokeLineOut(BaseModel):
    """One derived line (apps/lyrics/lines.py, the canonical grouping)."""

    model_config = ConfigDict(frozen=True)

    first_idx: int
    last_idx: int
    text: str
    start_s: float | None
    end_s: float | None
    n_words: int
    n_red: int
    n_judged: int
    quality: float | None = Field(
        default=None,
        description="share of witness-judged words NOT in the red classes; "
                    "null when no word in the line was judged",
    )
    band: str
    para_final: bool


class KaraokeTrackOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: CoverageVerdictOut
    words: list[KaraokeWordOut]
    lines: list[KaraokeLineOut] | None = Field(
        default=None, description="present only when requested with ?include=lines"
    )


class KaraokeSummaryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    counts: dict[str, int] = Field(description="effective-verdict histogram")
    total: int
    no_lyrics_max_coverage: float = Field(
        description="calibrated band: at or below this stem vocal coverage a track "
                    "is auto-stamped no-lyrics (novox round 0, pinned by test)"
    )
    sparse_max_coverage: float


class OverrideIn(BaseModel):
    override: str | None = Field(
        default=None, description="vocal|sparse|no-lyrics, or null to clear"
    )
    note: str | None = None


class LyricsConfigOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_order: list[str] = Field(
        description="lyric sources, strongest first; the pipeline's tie-break"
    )
    source_titles: dict[str, str] = Field(
        description="hover text per source (what the method actually does)"
    )
    is_default: bool = Field(
        description="true while no operator has persisted a custom order"
    )


class LyricsConfigIn(BaseModel):
    source_order: list[str]


class LyricJobOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    ts: str
    kind: str
    stable_ids: list[str]
    status: str
    note: str | None


class LyricJobIn(BaseModel):
    kind: str = Field(description="analyze | lyricsync | stems")
    stable_ids: list[str]
    note: str | None = None


class PurgeIn(BaseModel):
    source_prefix: str
    dry_run: bool = False


class PurgeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_prefix: str
    dry_run: bool
    rows_matched: int
    rows_tombstoned: int
    files_removed: int
    files_absent: int
    objects_deleted: int
    objects_absent: int
    r2_skipped_reason: str | None
    stable_ids: list[str]


def _require_state_db_path(request: Request) -> Path:
    path = getattr(request.app.state, "state_db_path", None)
    if path is None:
        raise AttributeError("state_db_path")
    return Path(path)


def _lyrics_read_conn(
    request: Request,
    _state_db_path: Path = Depends(_require_state_db_path),  # noqa: B008
) -> Iterator[sqlite3.Connection]:
    yield from get_cloudsync_conn(request)


def _lyrics_write_conn(
    request: Request,
    _state_db_path: Path = Depends(_require_state_db_path),  # noqa: B008
) -> Iterator[sqlite3.Connection]:
    yield from get_cloudsync_write_conn(request)


def _data_dir(request: Request) -> Path:
    return _require_state_db_path(request).parent.parent


def _state_dir(request: Request) -> Path:
    return _require_state_db_path(request).parent


def _asset_clients() -> tuple[Any, Any]:
    return artifacts.asset_clients_for_mode(writing=False)


def _out(v: store.LyricVerdict) -> CoverageVerdictOut:
    return CoverageVerdictOut(
        stable_id=v.stable_id,
        verdict=v.verdict,
        effective=v.effective,
        coverage_pct=v.coverage_pct,
        source=v.source,
        language_iso3=v.language_iso3,
        n_words=v.n_words,
        n_lines=v.n_lines,
        has_words=v.words_content_hash is not None,
        pct_witness_red=v.pct_witness_red,
        override=v.override,
        override_note=v.override_note,
        pipeline_version=v.pipeline_version,
        words_content_hash=v.words_content_hash,
        computed_at=v.computed_at,
        updated_at=v.updated_at,
    )


def _word_out(word: Any) -> KaraokeWordOut:
    return KaraokeWordOut(
        idx=word.idx,
        word=word.word,
        start_s=word.start_s,
        end_s=word.end_s,
        score=word.score,
        witness=word.witness,
        line_final=word.line_final,
        asr_delta_s=word.asr_delta_s,
    )


@router.get("/lyrics/summary", response_model=KaraokeSummaryOut)
def lyrics_summary(
    conn: sqlite3.Connection = Depends(_lyrics_read_conn),  # noqa: B008
) -> KaraokeSummaryOut:
    counts = store.count_verdicts(conn)
    return KaraokeSummaryOut(
        counts=counts,
        total=sum(counts.values()),
        no_lyrics_max_coverage=NO_LYRICS_MAX_COVERAGE,
        sparse_max_coverage=SPARSE_MAX_COVERAGE,
    )


@router.get("/lyrics", response_model=list[CoverageVerdictOut])
def list_lyrics(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    verdict: str | None = Query(default=None, description="filter by effective verdict"),
    order: str = Query(default="suspect", description="suspect|coverage|recent"),
    conn: sqlite3.Connection = Depends(_lyrics_read_conn),  # noqa: B008
    backend: StateBackend = Depends(get_read_state),  # noqa: B008
) -> list[CoverageVerdictOut]:
    try:
        rows = store.list_verdicts(
            conn, limit=limit, offset=offset, verdict=verdict, order=order
        )
    except store.LyricStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    tracks = backend.get_tracks_bulk([v.stable_id for v in rows])
    out: list[CoverageVerdictOut] = []
    for v in rows:
        track = tracks.get(v.stable_id)
        title = track.title if track is not None else None
        artist = track.artist if track is not None else None
        out.append(_out(v).model_copy(update={"title": title, "artist": artist}))
    return out


@router.get("/tracks/{stable_id}/lyrics/words", response_model=KaraokeTrackOut)
def get_track_lyrics_words(
    request: Request,
    stable_id: str,
    include: str | None = Query(
        default=None, description="'lines' adds the derived line objects"
    ),
    conn: sqlite3.Connection = Depends(_lyrics_read_conn),  # noqa: B008
) -> KaraokeTrackOut:
    if include not in (None, "lines"):
        raise HTTPException(status_code=422, detail=f"unknown include {include!r}")
    absence = store.verdict_absence_detail(conn, stable_id)
    if absence is not None:
        raise HTTPException(status_code=404, detail=absence)
    verdict = store.get_verdict(conn, stable_id)
    assert verdict is not None
    s3, cfg = _asset_clients()
    artifact = artifacts.load_words(
        conn,
        data_dir=_data_dir(request),
        stable_id=stable_id,
        s3=s3,
        cfg=cfg,
    )
    if artifact is None:
        raise HTTPException(
            status_code=404,
            detail=f"no readable karaoke_words artifact for {stable_id!r}",
        )
    words = [_word_out(word) for word in artifact.words]
    lines_out = None
    if include == "lines":
        lines_out = [
            KaraokeLineOut(**line.__dict__)
            for line in lyric_lines.derive_lines(artifact.words)
        ]
    return KaraokeTrackOut(verdict=_out(verdict), words=words, lines=lines_out)


@router.get("/lyrics/config", response_model=LyricsConfigOut)
def get_lyrics_config(request: Request) -> LyricsConfigOut:
    state_dir = _state_dir(request)
    try:
        order = sources_config.load_source_order(state_dir)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"persisted config invalid: {exc}")
    return LyricsConfigOut(
        source_order=order,
        source_titles=sources_config.SOURCE_TITLES,
        is_default=not sources_config.config_path(state_dir).is_file(),
    )


@router.put("/lyrics/config", response_model=LyricsConfigOut)
def put_lyrics_config(request: Request, body: LyricsConfigIn) -> LyricsConfigOut:
    state_dir = _state_dir(request)
    try:
        sources_config.save_source_order(state_dir, body.source_order)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return get_lyrics_config(request)


@router.get("/lyrics/jobs", response_model=list[LyricJobOut])
def get_lyric_jobs(request: Request) -> list[LyricJobOut]:
    jobs = lyric_jobs.list_jobs(_state_dir(request))
    return [LyricJobOut(**job.__dict__) for job in reversed(jobs)]


@router.post("/lyrics/jobs", response_model=LyricJobOut, status_code=201)
def post_lyric_job(
    request: Request,
    body: LyricJobIn,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008
) -> LyricJobOut:
    if len(body.stable_ids) > 500:
        raise HTTPException(status_code=422, detail="at most 500 tracks per job")
    known = backend.get_tracks_bulk(body.stable_ids)
    unknown = [sid for sid in body.stable_ids if sid not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown stable_ids: {unknown[:5]}")
    try:
        job = lyric_jobs.enqueue(
            _state_dir(request), body.kind, body.stable_ids, body.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LyricJobOut(**job.__dict__)


@router.put("/tracks/{stable_id}/lyrics/override", response_model=CoverageVerdictOut)
def put_override(
    stable_id: str,
    body: OverrideIn,
    conn: sqlite3.Connection = Depends(_lyrics_write_conn),  # noqa: B008
) -> CoverageVerdictOut:
    try:
        store.set_override(
            conn, stable_id=stable_id, override=body.override, note=body.note
        )
        updated = store.get_verdict(conn, stable_id)
    except store.LyricStoreError as exc:
        message = str(exc)
        if "no lyric_verdict row" in message or "was purged" in message:
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=422, detail=message) from exc
    assert updated is not None
    return _out(updated)


@router.post("/lyrics/purge", response_model=PurgeOut)
def post_lyrics_purge(
    request: Request,
    body: PurgeIn,
    conn: sqlite3.Connection = Depends(_lyrics_write_conn),  # noqa: B008
) -> PurgeOut:
    s3, cfg = artifacts.asset_clients_for_mode(writing=not body.dry_run)
    try:
        report = purge.purge_by_source(
            conn,
            data_dir=_data_dir(request),
            source_prefix=body.source_prefix,
            s3=s3,
            cfg=cfg,
            dry_run=body.dry_run,
        )
    except store.LyricStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PurgeOut(
        source_prefix=report.source_prefix,
        dry_run=report.dry_run,
        rows_matched=report.rows_matched,
        rows_tombstoned=report.rows_tombstoned,
        files_removed=report.files_removed,
        files_absent=report.files_absent,
        objects_deleted=report.objects_deleted,
        objects_absent=report.objects_absent,
        r2_skipped_reason=report.r2_skipped_reason,
        stable_ids=list(report.stable_ids),
    )
