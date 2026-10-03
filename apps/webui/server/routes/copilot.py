"""DJ Copilot endpoints (gating-wave unit: dj_copilot router).

CONTRACT (downstream ``play-it-sort-action`` builds against this):

  POST /api/v1/copilot/suggest-next
    body:  SuggestNextIn  {stable_id, session_ids?, top_n?, explain?}
    200:   SuggestNextOut {current, context_source, context_size,
                           candidates: [SuggestionOut],
                           pressure: PeakPressureOut}
    404:   ErrorBody {error: "not_found", message}   (unknown stable_id or
           unknown session_ids entry -- message names the id(s))
    422:   ErrorBody {error: "insufficient_data", message,
                      details: {stable_id, missing: {bpm: [ids],
                      key: [ids], energy: [ids]}}}
           when the CURRENT track is missing ``bpm`` or ``key`` (the two
           fields the harmonic ranker cannot score without). ``energy``
           is reported in ``missing`` when absent but does not alone
           trigger the 422: the engine documents energy as optional and
           missing energy yields ``unknown`` pressure rather than a 422.

  POST /api/v1/copilot/peak-pressure
    body:  PeakPressureIn {session_ids}
    200:   PeakPressureOut
    404:   ErrorBody {error: "not_found", message}

Semantics:

  * ``session_ids`` is the recently-played tail, ordered oldest first,
    most-recent LAST (mirrors :class:`SessionContext` "newest last").
    It drives the engine's artist cooldown + energy-slope filters.
  * Candidates come from the FULL track library served by the state
    backend; the engine's own filters (6% BPM window, artist cooldown,
    energy slope) then apply. ``candidates`` may legitimately be empty
    -- the UI must render that as an explicit state, never invent rows.
  * Stage-2 pairings re-rank reads the SAME state backend: pairings are
    projected into a throwaway in-memory sqlite table shaped like the
    Phase 8 schema (direction "->" -> "into", "<->" -> "either"), so
    ``pair_manual`` / ``pair_learned`` / ``pair_ai`` rationale tags work
    against both the InMemory and Sqlite backends. Real data, no mocks.
  * Keys are normalised via :func:`key_to_camelot`; an unparseable key
    is treated as missing (surfaces in the 422 when it is the current
    track's key).
  * Energy comes from track provenance (``_EAV_FIELDS`` includes
    ``energy``). Missing energy is optional and yields ``unknown``
    pressure rather than a 422.

Imports the dj_copilot engine directly (no subprocess), mirroring
``apps/dj_copilot/cli.py``'s suggest-next path.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps.dj_copilot.peak_pressure import PeakPlay, compute_peak_pressure
from apps.dj_copilot.play_it import InsufficientDataError
from apps.dj_copilot.session_context import PlayedTrack, SessionContext
from apps.dj_copilot.suggester import RankedSuggestion, suggest_next
from apps.shared.harmonic import TrackFeature, key_to_camelot

from ..backend import MAX_LIMIT, NotFoundError, StateBackend, Track, TrackFilter
from ..deps import get_read_state
from ..errors import ErrorBody

router = APIRouter(prefix="/copilot", tags=["copilot"])
_read_state = Depends(get_read_state)


# --- request / response models (defined here; models.py is integrator-owned)


class SuggestNextIn(BaseModel):
    stable_id: str = Field(..., description="Deck-loaded (current) track")
    session_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Recently played stable_ids, oldest first, most-recent last. "
            "Unknown ids are a 404, not silently dropped."
        ),
    )
    top_n: int = Field(10, ge=1, le=50)
    explain: bool = False


class CopilotTrackOut(BaseModel):
    stable_id: str
    title: str | None
    artist: str | None
    bpm: float | None
    key_camelot: str | None
    energy: int | None


class SuggestionOut(CopilotTrackOut):
    score: float
    rationale_tags: list[str]
    rationale_numbers: dict[str, float]
    explain_text: str | None


class PeakPressureOut(BaseModel):
    score: float
    cue: Literal["keep_building", "hold", "release", "unknown"]
    scored_tracks: int
    skipped_unknown: int
    cue_label: str
    advisory: str
    limitation: str


class PeakPressureIn(BaseModel):
    session_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Recently played stable_ids, oldest first, most-recent last. "
            "Unknown ids are a 404, not silently dropped."
        ),
    )


class SuggestNextOut(BaseModel):
    current: CopilotTrackOut
    context_source: str
    context_size: int
    candidates: list[SuggestionOut]
    pressure: PeakPressureOut


# ----------------------------------------------------------- _helpers


def _camelot_or_none(key: str | None) -> str | None:
    """Normalise any key notation to Camelot; unparseable -> missing."""
    if key is None or not key.strip():
        return None
    try:
        return str(key_to_camelot(key))
    except ValueError:
        return None


def _energy_of(track: Track) -> int | None:
    """Energy (MIK 1-10) from the provenance envelope; no rating fallback."""
    prov = (track.provenance or {}).get("energy")
    if prov is None or prov.value is None:
        return None
    return int(prov.value)


def _track_to_feature(track: Track) -> TrackFeature:
    return TrackFeature(
        stable_id=track.stable_id,
        artist=track.artist,
        bpm=track.bpm,
        key_camelot=_camelot_or_none(track.key),
        energy=_energy_of(track),
    )


def _load_library(backend: StateBackend) -> list[TrackFeature]:
    """Every track in the backend, paged at MAX_LIMIT until exhausted."""
    features: list[TrackFeature] = []
    cursor: str | None = None
    while True:
        page = backend.list_tracks(TrackFilter(cursor=cursor, limit=MAX_LIMIT))
        features.extend(_track_to_feature(t) for t in page.items)
        if page.next_cursor is None:
            return features
        cursor = page.next_cursor


def _preflight_current(feature: TrackFeature) -> None:
    """Raise InsufficientDataError when the current track cannot be scored.

    bpm + key gate the 422; energy is reported but optional (see module
    docstring).
    """
    missing: dict[str, list[str]] = {"bpm": [], "key": [], "energy": []}
    if feature.bpm is None:
        missing["bpm"].append(feature.stable_id)
    if not feature.key_camelot:
        missing["key"].append(feature.stable_id)
    if feature.energy is None:
        missing["energy"].append(feature.stable_id)
    if missing["bpm"] or missing["key"]:
        raise InsufficientDataError(
            playlist_id=f"suggest-next:{feature.stable_id}", missing=missing
        )


def _pairings_conn(
    backend: StateBackend, from_stable_id: str
) -> sqlite3.Connection:
    """Project backend pairings into the Phase 8 table shape for stage 2."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE pairings ("
        "  from_stable_id TEXT NOT NULL,"
        "  to_stable_id TEXT NOT NULL,"
        "  direction TEXT NOT NULL,"
        "  source TEXT NOT NULL)"
    )
    # An undirected edge can be stored with the current track second, as
    # the CLI does for (x, current, either); stage 2 reads it both ways.
    reverse_undirected = [
        p for p in backend.list_pairings(to_stable_id=from_stable_id)
        if p.direction == "<->"
    ]
    for pairing in [
        *backend.list_pairings(from_stable_id=from_stable_id),
        *reverse_undirected,
    ]:
        if pairing.direction == "->":
            direction = "into"
        elif pairing.direction == "<->":
            direction = "either"
        else:
            raise AssertionError(
                f"unhandled pairing direction: {pairing.direction!r}"
            )
        conn.execute(
            "INSERT INTO pairings "
            "(from_stable_id, to_stable_id, direction, source) "
            "VALUES (?, ?, ?, ?)",
            (pairing.from_stable_id, pairing.to_stable_id, direction,
             pairing.source),
        )
    return conn


def _resolve_session_tracks(
    backend: StateBackend, session_ids: list[str]
) -> list[Track]:
    found = backend.get_tracks_bulk(session_ids)
    missing_ids = [sid for sid in session_ids if sid not in found]
    if missing_ids:
        raise NotFoundError(
            f"session tracks not found: {', '.join(missing_ids)}"
        )
    return [found[sid] for sid in session_ids]


def _track_to_out(track: Track, feature: TrackFeature) -> CopilotTrackOut:
    return CopilotTrackOut(
        stable_id=track.stable_id,
        title=track.title,
        artist=track.artist,
        bpm=feature.bpm,
        key_camelot=feature.key_camelot,
        energy=feature.energy,
    )


def _suggestion_to_out(
    suggestion: RankedSuggestion, title: str | None
) -> SuggestionOut:
    cand = suggestion.candidate
    if cand is None:  # engine always attaches the feature; broken invariant
        raise AssertionError(
            f"suggestion {suggestion.stable_id!r} has no candidate feature"
        )
    return SuggestionOut(
        stable_id=suggestion.stable_id,
        title=title,
        artist=cand.artist,
        bpm=cand.bpm,
        key_camelot=cand.key_camelot,
        energy=cand.energy,
        score=suggestion.score,
        rationale_tags=list(suggestion.rationale_tags),
        rationale_numbers=dict(suggestion.rationale_numbers),
        explain_text=suggestion.explain_text,
    )


def _plays_from_tracks(tracks: list[Track]) -> list[PeakPlay]:
    return [
        PeakPlay(
            stable_id=t.stable_id,
            energy=_energy_of(t),
            tags=tuple(t.tags or ()),
        )
        for t in tracks
    ]


def _pressure_out(session_tracks: list[Track]) -> PeakPressureOut:
    result = compute_peak_pressure(_plays_from_tracks(session_tracks))
    return PeakPressureOut(
        score=result.score,
        cue=result.cue,
        scored_tracks=result.scored_tracks,
        skipped_unknown=result.skipped_unknown,
        cue_label=result.cue_label,
        advisory=result.advisory,
        limitation=result.limitation,
    )


# ----------------------------------------------------------- route


@router.post(
    "/suggest-next",
    response_model=SuggestNextOut,
    responses={
        404: {"model": ErrorBody, "description": "unknown stable_id / session id"},
        422: {"model": ErrorBody, "description": "insufficient analysis data"},
    },
)
def suggest_next_route(
    body: SuggestNextIn,
    backend: StateBackend = _read_state,
) -> SuggestNextOut | JSONResponse:
    # Unknown current track -> NotFoundError -> 404 via app handler.
    current_track = backend.get_track(body.stable_id)
    session_tracks = _resolve_session_tracks(backend, body.session_ids)

    current_feature = _track_to_feature(current_track)
    try:
        _preflight_current(current_feature)
    except InsufficientDataError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=ErrorBody(
                error="insufficient_data",
                message=str(exc),
                details={"stable_id": body.stable_id, "missing": exc.missing},
            ).model_dump(),
        )

    now = datetime.now(UTC)
    if session_tracks:
        context = SessionContext(
            recent=[
                PlayedTrack(
                    stable_id=t.stable_id,
                    artist=t.artist,
                    bpm=t.bpm,
                    key_camelot=_camelot_or_none(t.key),
                    energy=_energy_of(t),
                    played_at=now,
                    tags=tuple(t.tags or ()),
                )
                for t in session_tracks
            ],
            source="manual",
            captured_at=now,
        )
    else:
        context = SessionContext(recent=[], source="empty", captured_at=now)

    library = _load_library(backend)
    conn = _pairings_conn(backend, body.stable_id)
    try:
        suggestions = suggest_next(
            conn=conn,
            current_stable_id=body.stable_id,
            library=library,
            context=context,
            top_n=body.top_n,
            explain=body.explain,
        )
    finally:
        conn.close()

    titles = backend.get_tracks_bulk([s.stable_id for s in suggestions])
    return SuggestNextOut(
        current=_track_to_out(current_track, current_feature),
        context_source=context.source,
        context_size=len(context.recent),
        candidates=[
            _suggestion_to_out(s, titles[s.stable_id].title)
            for s in suggestions
        ],
        pressure=_pressure_out(session_tracks),
    )


@router.post(
    "/peak-pressure",
    response_model=PeakPressureOut,
    responses={
        404: {"model": ErrorBody, "description": "unknown session id"},
    },
)
def peak_pressure_route(
    body: PeakPressureIn,
    backend: StateBackend = _read_state,
) -> PeakPressureOut | JSONResponse:
    session_tracks = _resolve_session_tracks(backend, body.session_ids)
    return _pressure_out(session_tracks)
