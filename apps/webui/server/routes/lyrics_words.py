"""Karaoke WORD timings, verdict triage and the operator levers (LYR-03).

Sibling of ``routes/tracks.py``'s ``GET /tracks/{stable_id}/lyrics``, which
serves the LINE-level LRCLIB cache (:mod:`apps.lyrics.cache`) and is left
byte-untouched by this module. The two never merge: lines are a fetched
display artifact, words are the aligned karaoke timeline whose only location
record is ``lyric_verdict.words_content_hash`` (D13.2).

Agent-native parity (house requirement): every action the karaoke UI offers
has an endpoint here - read words, triage the library, override a verdict,
reorder the sources, queue offline work, purge a provider, read the KPI
ledger - so the whole flow is drivable with curl and testable without a
browser.

**404 is the honest missing state.** There is no ``on_missing=null``
parameter: a track with no verdict row, or a verdict row whose words this
machine cannot produce, is a 404 whose detail says WHICH of those it is. A
200 carrying an empty envelope would make "nothing to show" and "something is
wrong" the same response.

**Connections.** Reads use :func:`get_lyrics_read_conn`, which resolves
``request.app.state.state_db_path`` with NO ``data/state/state.db`` default:
a daemon built without that attribute must raise AttributeError, not read (or
create) a DB relative to whatever directory it was started from.
``lyric_verdict`` is a SYNCED table, so the two endpoints that write rows
(override, purge) go through ``routes/cloudsync.py``'s write dependency,
which is the connection ``apps.lyrics.store`` documents as the only one whose
writes get stamped and logged.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from apps.cloud.eviction import HydrationError
from apps.lyrics import artifacts, karaoke_cache, library_verdicts, sources_config, store
from apps.lyrics import jobs as lyric_jobs
from apps.lyrics import lines as lyric_lines
from apps.lyrics import purge as lyric_purge
from apps.lyrics.vocal_presence import NO_LYRICS_MAX_COVERAGE, SPARSE_MAX_COVERAGE
from apps.shared.state.schema import LYRIC_OVERRIDES

from .cloudsync import get_cloudsync_write_conn
from .lyrics_words_models import (
    CoverageVerdictOut,
    KaraokeLineOut,
    KaraokeSummaryOut,
    KaraokeTrackOut,
    KaraokeWordOut,
    LyricJobIn,
    LyricJobOut,
    LyricsConfigIn,
    LyricsConfigOut,
    LyricsKpiOut,
    LyricsPurgeIn,
    LyricsPurgeOut,
    LyricsVerdictBackfillIn,
    LyricsVerdictBackfillOut,
    OverrideIn,
)

router = APIRouter(tags=["lyrics"])

REPO_ROOT: Path = Path(__file__).resolve().parents[4]
#: Module-level so a test can point it at a tmp ledger.
LYRICS_KPI_LEDGER_FILE: Path = REPO_ROOT / "scripts" / "bench" / "lyrics_kpi_ledger.json"
MAX_JOB_TRACKS: int = 500
#: Named because no CLI subcommand hydrates ONE track's words on demand
#: (``python -m apps.lyrics`` has fetch / index / ingest-state /
#: migrate-legacy-words / purge and the scoring commands, none of which pull a
#: single artifact back from R2). Saying so beats inventing a command.
NO_SINGLE_TRACK_HYDRATE: str = (
    "no CLI subcommand hydrates one track's words on demand; re-run the batch "
    "that produced them (`python -m apps.lyrics ingest-state --manifest <run> "
    "--write`) or copy the artifact into the cache path above"
)


#-----------------------------------------------------------------------------
# connections + paths
#-----------------------------------------------------------------------------
def _state_db_path(request: Request) -> Path:
    """The configured state DB. NO default, deliberately.

    ``data/state/state.db`` as a fallback is relative, so a daemon started
    from the wrong directory would quietly serve (and write) a different
    library's verdicts. Reading the attribute directly means a misconfigured
    app raises AttributeError on the first request instead.
    """
    return Path(request.app.state.state_db_path)


def _state_dir(request: Request) -> Path:
    """Directory holding state.db: also home to lyrics-config.json and
    lyrics-jobs.json, both read by the offline runners."""
    return _state_db_path(request).parent


def _data_dir(request: Request) -> Path:
    """The data dir the karaoke cache hangs off (state.db is
    ``<data_dir>/state/state.db``), same derivation as ``routes/tracks.py``."""
    return _state_db_path(request).parent.parent


def get_lyrics_read_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Read-only sqlite handle for every karaoke READ.

    ``query_only`` makes the read-only intent enforced rather than promised.
    ``check_same_thread=False`` because FastAPI runs sync dependencies on a
    threadpool (same reasoning as ``routes/cloudsync.py``).
    """
    db_path = _state_db_path(request)
    if not db_path.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"state DB not found at {db_path}; initialise it with "
                   "`python -m apps.shared.state.cli init`",
        )
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, isolation_level=None,
        check_same_thread=False,
    )
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


#-----------------------------------------------------------------------------
# shaping helpers
#-----------------------------------------------------------------------------
def _verdict_out(verdict: store.LyricVerdict) -> CoverageVerdictOut:
    return CoverageVerdictOut(
        stable_id=verdict.stable_id,
        verdict=verdict.verdict,
        effective_verdict=verdict.effective,
        effective=verdict.effective,
        coverage_pct=verdict.coverage_pct,
        source=verdict.source,
        language_iso3=verdict.language_iso3,
        n_words=verdict.n_words,
        n_lines=verdict.n_lines,
        pct_witness_red=verdict.pct_witness_red,
        has_words=verdict.words_content_hash is not None,
        override=verdict.override,
        override_note=verdict.override_note,
        pipeline_version=verdict.pipeline_version,
        computed_at=verdict.computed_at,
        updated_at=verdict.updated_at,
    )


def _missing_words_detail(verdict: store.LyricVerdict, cache: Path) -> str:
    """Two different failures, told apart. A row with no hash was never
    aligned; a row WITH a hash whose bytes are absent is a hydration gap, and
    conflating them sends an operator looking in the wrong place."""
    if verdict.words_content_hash is None:
        return (
            f"no karaoke words artifact for track {verdict.stable_id!r} "
            f"(verdict {verdict.effective}, no words_content_hash)"
        )
    return (
        f"karaoke words artifact {verdict.words_content_hash} for track "
        f"{verdict.stable_id!r} is not on this machine: nothing at {cache} and "
        f"cloudsync mode is local, so R2 was not consulted. {NO_SINGLE_TRACK_HYDRATE}"
    )


def _hydration_detail(
    verdict: store.LyricVerdict, cache: Path, error: HydrationError
) -> str:
    return (
        f"karaoke words artifact {verdict.words_content_hash} for track "
        f"{verdict.stable_id!r} could not be served from {cache}: {error}. "
        f"{NO_SINGLE_TRACK_HYDRATE}"
    )


def _titles_by_stable_id(
    conn: sqlite3.Connection, stable_ids: list[str]
) -> dict[str, tuple[str | None, str | None]]:
    """The listing's title/artist join.

    One of exactly two raw statements in this router, both reads of ``tracks``
    (the other is the job queue's existence check). Everything touching
    ``lyric_verdict`` goes through :mod:`apps.lyrics.store`, which is what
    filters tombstones; ``store`` has no business joining track metadata, and
    the triage table is unusable showing bare stable_ids.
    ``backend.get_tracks_bulk`` is not used for either: it hydrates the EAV
    field map this router has no use for, and falls back with a warning when
    ``tracks`` is absent, which is exactly the silence this router refuses.

    A malformed ``artists_json`` raises rather than degrading to a blank
    artist: our own ingest writes that column, so bad JSON is a bug to see.
    """
    if not stable_ids:
        return {}
    marks = ",".join("?" for _ in stable_ids)
    out: dict[str, tuple[str | None, str | None]] = {}
    for row in conn.execute(
        f"SELECT stable_id, title, artists_json FROM tracks "
        f"WHERE stable_id IN ({marks}) AND deleted_at IS NULL",
        stable_ids,
    ):
        artists = json.loads(row[2]) if row[2] else []
        joined = ", ".join(str(name) for name in artists if name) or None
        out[row[0]] = (row[1], joined)
    return out


#-----------------------------------------------------------------------------
# words
#-----------------------------------------------------------------------------
@router.get("/tracks/{stable_id}/lyrics/words", response_model=KaraokeTrackOut)
def get_track_karaoke_words(
    request: Request,
    stable_id: str,
    include: str | None = Query(
        default=None, description="'lines' adds the derived line objects"
    ),
    conn: sqlite3.Connection = Depends(get_lyrics_read_conn),  # noqa: B008
) -> KaraokeTrackOut:
    """This track's karaoke timeline: the verdict envelope plus every word."""
    if include not in (None, "lines"):
        raise HTTPException(
            status_code=422,
            detail=f"unknown include {include!r}; the only supported value is 'lines'",
        )
    verdict = store.get_verdict(conn, stable_id)
    if verdict is None:
        raise HTTPException(
            status_code=404, detail=f"no karaoke lyric data for track {stable_id!r}"
        )
    data_dir = _data_dir(request)
    cache = karaoke_cache.cache_path(data_dir, stable_id)
    s3, cfg = artifacts.asset_clients_for_mode(writing=False)
    try:
        words = artifacts.load_words(
            conn, data_dir=data_dir, stable_id=stable_id, s3=s3, cfg=cfg
        )
    except HydrationError as exc:
        raise HTTPException(
            status_code=404, detail=_hydration_detail(verdict, cache, exc)
        ) from exc
    if words is None:
        raise HTTPException(
            status_code=404, detail=_missing_words_detail(verdict, cache)
        )
    lines_out = None
    if include == "lines":
        lines_out = [
            KaraokeLineOut(**line.__dict__)
            for line in lyric_lines.derive_lines(words.words)
        ]
    return KaraokeTrackOut(
        verdict=_verdict_out(verdict),
        words=[
            KaraokeWordOut(
                idx=word.idx, word=word.word, start_s=word.start_s, end_s=word.end_s,
                score=word.score, witness=word.witness, line_final=word.line_final,
            )
            for word in words.words
        ],
        lines=lines_out,
    )


#-----------------------------------------------------------------------------
# triage: summary, listing, override
#-----------------------------------------------------------------------------
@router.get("/lyrics/summary", response_model=KaraokeSummaryOut)
def get_lyrics_summary(
    conn: sqlite3.Connection = Depends(get_lyrics_read_conn),  # noqa: B008
) -> KaraokeSummaryOut:
    """Library-wide verdict histogram plus the calibrated bands, so a client
    never hardcodes the thresholds it displays."""
    counts = store.count_verdicts(conn)
    return KaraokeSummaryOut(
        counts=counts,
        total=sum(counts.values()),
        no_lyrics_max_coverage=NO_LYRICS_MAX_COVERAGE,
        sparse_max_coverage=SPARSE_MAX_COVERAGE,
    )


@router.get("/lyrics", response_model=list[CoverageVerdictOut])
def list_lyric_verdicts(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    verdict: str | None = Query(default=None, description="filter by effective verdict"),
    order: str = Query(default="suspect", description="suspect|coverage|recent"),
    conn: sqlite3.Connection = Depends(get_lyrics_read_conn),  # noqa: B008
) -> list[CoverageVerdictOut]:
    """The triage listing: least trustworthy first by default, with the track
    names joined in so a human can act on a row without a second request."""
    try:
        rows = store.list_verdicts(
            conn, limit=limit, offset=offset, verdict=verdict, order=order
        )
    except store.LyricStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    meta = _titles_by_stable_id(conn, [row.stable_id for row in rows])
    return [
        _verdict_out(row).model_copy(
            update=dict(zip(("title", "artist"), meta.get(row.stable_id, (None, None)),
                            strict=True))
        )
        for row in rows
    ]


@router.put("/tracks/{stable_id}/lyrics/override", response_model=CoverageVerdictOut)
def put_lyric_override(
    stable_id: str,
    body: OverrideIn,
    conn: sqlite3.Connection = Depends(get_cloudsync_write_conn),  # noqa: B008
) -> CoverageVerdictOut:
    """Set (or clear with null) the human verdict, which always beats the
    computed one wherever it is read (``store.LyricVerdict.effective``).

    The override VALUE is validated before the write so the two failures stay
    distinguishable: an unknown value is the caller's mistake (422), while a
    refusal from the store means there is no live row to override (404).
    """
    if body.override is not None and body.override not in LYRIC_OVERRIDES:
        raise HTTPException(
            status_code=422,
            detail=f"override {body.override!r} not in {list(LYRIC_OVERRIDES)}",
        )
    try:
        store.set_override(
            conn, stable_id=stable_id, override=body.override, note=body.note
        )
    except store.LyricStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    updated = store.get_verdict(conn, stable_id)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"lyric_verdict {stable_id!r} vanished between write and read",
        )
    return _verdict_out(updated)


#-----------------------------------------------------------------------------
# operator config + job queue
#-----------------------------------------------------------------------------
@router.get("/lyrics/config", response_model=LyricsConfigOut)
def get_lyrics_config(request: Request) -> LyricsConfigOut:
    """The source ordering the pipeline will use on its next run.

    No DB connection: the order is a JSON file beside state.db, read by BOTH
    this endpoint and the offline scripts, so what the admin panel shows is
    what the next fetch uses.
    """
    state_dir = _state_dir(request)
    try:
        order = sources_config.load_source_order(state_dir)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"persisted lyrics-config.json is invalid: {exc}",
        ) from exc
    return LyricsConfigOut(
        source_order=order,
        source_titles=sources_config.SOURCE_TITLES,
        is_default=not sources_config.config_path(state_dir).is_file(),
    )


@router.put("/lyrics/config", response_model=LyricsConfigOut)
def put_lyrics_config(request: Request, body: LyricsConfigIn) -> LyricsConfigOut:
    """Persist a full reordering. Partial lists are rejected: an ordering that
    silently omits a source is an ordering the operator never saw."""
    try:
        sources_config.save_source_order(_state_dir(request), body.source_order)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return get_lyrics_config(request)


@router.get("/lyrics/jobs", response_model=list[LyricJobOut])
def get_lyric_jobs(request: Request) -> list[LyricJobOut]:
    """Newest first. The queue is drained by the offline runner scripts:
    stems and alignment never run in the daemon (house rule)."""
    jobs = lyric_jobs.list_jobs(_state_dir(request))
    return [LyricJobOut(**job.__dict__) for job in reversed(jobs)]


@router.post("/lyrics/jobs", response_model=LyricJobOut, status_code=201)
def post_lyric_job(
    request: Request,
    body: LyricJobIn,
    conn: sqlite3.Connection = Depends(get_cloudsync_write_conn),  # noqa: B008
) -> LyricJobOut:
    """Record a processing request from the UI (context menu / admin panel).

    Every stable_id is checked against ``tracks`` in ONE select (the second and
    last raw statement here, see :func:`_titles_by_stable_id`): a queue entry
    for a track the library does not know is a job no runner can honour.
    """
    if len(body.stable_ids) > MAX_JOB_TRACKS:
        raise HTTPException(
            status_code=422, detail=f"at most {MAX_JOB_TRACKS} tracks per job"
        )
    marks = ",".join("?" for _ in body.stable_ids) or "''"
    known = {
        row[0]
        for row in conn.execute(
            f"SELECT stable_id FROM tracks WHERE stable_id IN ({marks}) AND deleted_at IS NULL",
            body.stable_ids,
        )
    }
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


#-----------------------------------------------------------------------------
# licensing purge + the KPI ledger
#-----------------------------------------------------------------------------
@router.post("/lyrics/purge", response_model=LyricsPurgeOut)
def post_lyrics_purge(
    request: Request,
    body: LyricsPurgeIn,
    conn: sqlite3.Connection = Depends(get_cloudsync_write_conn),  # noqa: B008
) -> LyricsPurgeOut:
    """Remove one provider's lyrics from the row, the disk and R2 - parity for
    the ``python -m apps.lyrics purge`` CLI, same function underneath.

    ``writing=not dry_run`` is how the CLI asks for credentials too: a
    rehearsal pushes and deletes nothing, so it must stay runnable on a
    machine that has none.
    """
    s3, cfg = artifacts.asset_clients_for_mode(writing=not body.dry_run)
    try:
        report = lyric_purge.purge_by_source(
            conn,
            data_dir=_data_dir(request),
            source_prefix=body.source_prefix,
            s3=s3,
            cfg=cfg,
            dry_run=body.dry_run,
        )
    except store.LyricStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LyricsPurgeOut(**report.__dict__)


@router.post("/lyrics/verdicts/backfill", response_model=LyricsVerdictBackfillOut)
def post_lyrics_verdicts_backfill(
    request: Request,
    body: LyricsVerdictBackfillIn,
    conn: sqlite3.Connection = Depends(get_cloudsync_write_conn),  # noqa: B008
) -> LyricsVerdictBackfillOut:
    """Parity for ``python -m apps.lyrics verdicts backfill`` (LYR-06): fill
    or refresh the stem-coverage-only verdict for every loadable bundle,
    agent-native so the library-scale backfill is drivable without the CLI.

    A track whose row already carries word-level data (``words_content_hash``)
    is reported skipped, never overwritten - this endpoint only ever fills or
    refreshes the coverage fields, same contract as the CLI.
    """
    try:
        report = library_verdicts.backfill_verdicts(
            conn,
            data_dir=_data_dir(request),
            dry_run=body.dry_run,
            limit=body.limit,
            include_reserved=body.include_reserved,
        )
    except (store.LyricStoreError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LyricsVerdictBackfillOut(**library_verdicts.report_to_dict(report))


@router.get("/bench/lyrics-kpi", response_model=LyricsKpiOut)
def get_lyrics_kpi_ledger() -> LyricsKpiOut:
    """The lyrics KPI ledger verbatim (``scripts/bench/lyrics_kpi_ledger.json``).

    Read-only here: appending a snapshot is a CLI act
    (``scripts/bench/lyrics_kpi_append.py``) so the ledger keeps one writer.
    A missing key raises rather than defaulting - a KPI panel drawing invented
    numbers is worse than a panel that fails.
    """
    if not LYRICS_KPI_LEDGER_FILE.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"lyrics KPI ledger not found at {LYRICS_KPI_LEDGER_FILE}; it is "
                   "appended by scripts/bench/lyrics_kpi_append.py",
        )
    payload: dict[str, Any] = json.loads(
        LYRICS_KPI_LEDGER_FILE.read_text(encoding="utf-8")
    )
    return LyricsKpiOut(kpis=payload["kpis"], snapshots=payload["snapshots"])


__all__ = ["get_lyrics_read_conn", "router"]
