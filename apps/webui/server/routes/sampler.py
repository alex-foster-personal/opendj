"""Edge sampler routes: the ranked, paged catalogue, slot edits, captures, sets, pins and clips (EDGESAMP-01..03, 15, 19, 20).

Spec: ``(private path)`` sections 3.2 (slot, vector), 3.4 (ranking), 7.4 (drag a loop in, sets, pins,
edit) and 10 (parity).
Wire contract: ``apps/webui/frontend/src/lib/player/sampler/sampler-types.ts`` (snake_case).

  * ``GET /api/v1/sampler/slots``            -> SlotsPage (sort, q, offset, limit, master_bpm, master_key)
  * ``GET /api/v1/sampler/slots/{slot_id}``  -> SamplerSlot, 404 SLOT_NOT_FOUND
  * ``PATCH /api/v1/sampler/slots/{slot_id}`` -> SamplerSlot (name, description, settings)
  * ``POST /api/v1/sampler/slots``           -> 201 SamplerSlot (capture a deck loop and its mix vector)
  * ``DELETE /api/v1/sampler/slots/{slot_id}`` -> 204, captures only (409 NOT_A_CAPTURE for cue slots)
  * ``GET /api/v1/sampler/sets``             -> {sets: SetMeta[]}
  * ``PUT /api/v1/sampler/sets/{set_id}``    -> SetMeta
  * ``GET /api/v1/sampler/pins``             -> {pins: slot_id[]} (oldest first)
  * ``PUT /api/v1/sampler/pins/{slot_id}``   {pinned} -> {pins}; pinning an unknown slot is 404
  * ``GET /api/v1/sampler/clips/{slot_id}``  -> audio/flac, region + 20 ms guard, X-Guard-Ms
    (a ``classic:`` id: a classic bank pad's whole source, ``sampler_classic_clips``, 404 when empty or gone)
    ``?part=vocals|drums|bass|inst`` cuts that part from the track's stem-set instead (EDGESAMP-11872h)
  * ``GET /api/v1/sampler/banks``            -> the classic sampler's 4 x 16 banks, each slot marked ``missing``
  * ``PUT /api/v1/sampler/banks``            -> replaces the banks document atomically, 422 on a bad mode or gain
    (slot gain -24..+6 dB, master_gain_db -60..0 dB, 0 dB when left out, #9989)
  * ``POST /api/v1/sampler/captures``        audio/wav -> {path, duration_ms}, 422 not a WAV, 413 over the cap

The catalogue is ``sampler_catalogue.build_catalogue``; ranking and the text filter are
``sampler_core``; edits, sets and pins persist in ``sampler.db`` (``apps.shared.state.sampler_store``).
A capture is a row (region on the track's own grid plus the deck's mix vector), never a new audio
file; the drag-in and the agent's ``sampler_capture`` both land here. Clips are rendered by ``sampler_clips``.

Mini-PRD:
  ✔︎ ✅ 🎯 GET /slots lists every grid-snapped slot, ranked and paged, with skipped_no_grid
    [if] sort=compat with a playing master [then] in-window BPM slots come first [else ⛔️]
    [if] offset and limit page the list [then] the pages concatenate to the whole ranked list [else ⛔️]
    [if] q matches nothing [then] total is 0 [else ⛔️]
  ✔︎ ✅ 🎯 GET /slots/{slot_id} returns the listed slot, 404 for an unknown id
  ✔︎ ✅ 🎯 PATCH /slots/{slot_id} persists name, description and settings across a restart
    [if] a slot is renamed [then] a fresh client reads the new name and the source title [else ⛔️]
    [if] a name is over 60 chars or a level over 100 [then] 422 and nothing is stored [else ⛔️]
  ✔︎ ✅ 🎯 sets and pins persist; pinning needs a live slot, unpinning never does
    [if] a set is put [then] GET /sets returns it [else ⛔️]
    [if] an unknown slot is pinned [then] 404 and no pin is stored [else ⛔️]
  ✔︎ ✅ 🎯 POST /slots captures a loop region and its vector; DELETE /slots/{slot_id} removes a capture
    [if] a capture is posted 240 ms off the beat [then] GET /slots lists it on the beat with its vector [else ⛔️]
    [if] a capture is deleted [then] GET /slots no longer lists it and its pin goes too [else ⛔️]
    [if] a capture lands on an occupied place of a stored set [then] it is inserted there, nothing lost [else ⛔️]
    [if] a loop hot cue slot is deleted [then] 409 NOT_A_CAPTURE [else ⛔️]
  ✔︎ ✅ 🎯 GET /clips/{slot_id} serves the slot's flac clip, rendered once (``sampler_clips``)
    [if] an unknown slot's clip is fetched [then] 404 SLOT_NOT_FOUND [else ⛔️]
  ✔︎ ✅ 🎯 sort=crate groups by the track's first crate in library order, sort=color by palette order
    [if] sort=crate [then] a crate's slots are contiguous, crates in library order, crate-less last [else ⛔️]
    [if] sort=color [then] slots group by palette order of their colour, colourless last [else ⛔️]
    [if] sort names no known order [then] 422, never a silent default [else ⛔️]
  ✔︎ ✅ 🎯 GET/PUT /banks persist the classic sampler banks (``apps.shared.state.sampler_banks``, #9994)
    [if] no banks were ever put [then] GET answers 4 x 16 empty banks [else ⛔️]
    [if] a slot has an unknown mode or a gain outside -24..+6 dB [then] 422 and nothing is stored [else ⛔️]
    [if] a slot's stable_id no longer resolves [then] GET keeps it with missing true [else ⛔️]
  ✔︎ ✅ 🎯 POST /captures stores a rendered loop WAV a bank slot's path reloads (``sampler_captures``, #9998)
    [if] a WAV is posted [then] the returned path, put in a bank slot, reloads from a fresh client [else ⛔️]
    [if] the body is not a WAV or is over the cap [then] 422 or 413 and nothing is stored [else ⛔️]
"""

from __future__ import annotations

from contextlib import suppress
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state import sampler_banks, sampler_captures, sampler_store
from apps.shared.state.sync_stamp import canonical_now

from ..backend import StateBackend
from ..deps import get_read_state
from ..sampler_bank_models import BanksPut, BanksView, CaptureStored
from ..sampler_catalogue import build_catalogue
from ..sampler_classic_clips import classic_clip, is_classic
from ..sampler_clips import ClipPart, cached_clip, rendered_clip, serve_clip
from ..sampler_core import (
    SOURCE_CAPTURE,
    SlotSort,
    crate_positions,
    matches_query,
    rank,
    slot_id_of,
    snap_region,
    usable_grid,
)
from ..sampler_models import CaptureRequest, PinPut, SetPut, SlotPatch
from ..sampler_sources import sources_for
from .grid_edit import state_db_path

router = APIRouter(prefix="/sampler", tags=["sampler"])
MOUNT_UNDER_API_PREFIX = True

ReadState = Annotated[StateBackend, Depends(get_read_state)]


@router.get("/slots")
def list_slots(
    request: Request,
    backend: ReadState,
    sort: SlotSort = "compat",
    q: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=5000),
    master_bpm: float | None = Query(None, gt=0),
    master_key: str | None = None,
) -> dict[str, Any]:
    """One page of the ranked slot catalogue, its total, skipped_no_grid and rekordbox's status."""
    cat = build_catalogue(request, backend)
    slots = [s for s in cat.slots if matches_query(s, q)] if q else cat.slots
    # Only sort=crate reads the playlists: the other orders never pay for the library's crate walk.
    crate_of = crate_positions(sources_for(request).crates(request, backend)) if sort == "crate" else {}
    ranked = rank(slots, sort, master_bpm, master_key, cat.recency, crate_of)
    return {
        "slots": ranked[offset : offset + limit],
        "total": len(ranked),
        "skipped_no_grid": cat.skipped_no_grid,
        "rekordbox": cat.rekordbox,
    }


def _find_slot(request: Request, backend: StateBackend, slot_id: str) -> dict[str, Any]:
    for slot in build_catalogue(request, backend).slots:
        if slot["slot_id"] == slot_id:
            return slot
    raise HTTPException(status_code=404, detail={"code": "SLOT_NOT_FOUND", "message": f"no sampler slot {slot_id}"})


def _published(stable_id: str) -> None:
    publish("library.changed", {"kind": "sampler", "ids": [stable_id]})


@router.get("/slots/{slot_id}")
def get_slot(slot_id: str, request: Request, backend: ReadState) -> dict[str, Any]:
    """The listed slot with this id, else 404 SLOT_NOT_FOUND."""
    return _find_slot(request, backend, slot_id)


@router.patch("/slots/{slot_id}")
def patch_slot(slot_id: str, body: SlotPatch, request: Request, backend: ReadState) -> dict[str, Any]:
    """Save a slot's name, description and settings (an omitted field keeps its value; an empty name clears it)."""
    slot = _find_slot(request, backend, slot_id)
    name = (body.name or None) if body.name is not None else slot["name"]
    description = body.description if body.description is not None else slot["description"]
    settings = body.settings.model_dump() if body.settings is not None else slot["settings"]
    sampler_store.upsert_meta(
        state_db_path(request),
        slot_id,
        slot["stable_id"],
        name=name,
        description=description,
        settings=settings,
        updated_at=canonical_now(),
    )
    _published(slot["stable_id"])
    # Only the three written fields change, so the second catalogue build is skipped.
    return {**slot, "name": name, "description": description, "settings": settings}


@router.post("/slots", status_code=201)
def capture_slot(body: CaptureRequest, request: Request, backend: ReadState) -> dict[str, Any]:
    """Capture a deck loop: snap it to the track's grid, store it with its vector, place it in the set."""
    _require_live_track(request, body.stable_id)
    analysis = sources_for(request).analysis(request, body.stable_id)
    if not usable_grid(analysis.beats_ms):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "NO_BEATGRID",
                "message": f"track {body.stable_id} has no usable beatgrid; a capture needs one",
            },
        )
    region = snap_region(analysis.beats_ms, body.in_ms, body.out_ms, body.beats)
    if region is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "REGION_OFF_GRID", "message": f"{body.beats} beats from {body.in_ms} ms run past the grid"},
        )
    slot_id = slot_id_of(body.stable_id, SOURCE_CAPTURE, region)
    sampler_store.upsert_capture(
        state_db_path(request),
        body.stable_id,
        in_ms=region.in_ms,
        out_ms=region.out_ms,
        beats=region.beats,
        vector=body.vector.model_dump(),
        created_at=canonical_now(),
    )
    _insert_into_set(request, body.set_id, body.index, slot_id)
    _published(body.stable_id)
    return _find_slot(request, backend, slot_id)


def _require_live_track(request: Request, stable_id: str) -> None:
    conn = state_db.open_ro(state_db_path(request))
    try:
        live = conn.execute("SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL", (stable_id,)).fetchone()
    finally:
        conn.close()
    if live is None:
        raise HTTPException(
            status_code=404, detail={"code": "TRACK_NOT_FOUND", "message": f"no live track {stable_id}"}
        )


def _insert_into_set(request: Request, set_id: str, index: int, slot_id: str) -> None:
    """A set with a stored layout gets the capture at ``index``, the rest move down one; a ranked set ranks it in."""
    db = state_db_path(request)
    for stored in sampler_store.read_sets(db):
        if stored["id"] == set_id and stored["slot_ids"] is not None:
            ids = [i for i in stored["slot_ids"] if i != slot_id]  # a re-capture moves, never duplicates
            ids.insert(min(index, len(ids)), slot_id)
            sampler_store.write_set(
                db,
                set_id,
                name=stored["name"],
                description=stored["description"],
                slot_ids=ids,
                updated_at=canonical_now(),
            )


@router.delete("/slots/{slot_id}", status_code=204)
def delete_slot(slot_id: str, request: Request, backend: ReadState) -> Response:
    """Delete a capture with its meta and pin; a cue slot is 409 NOT_A_CAPTURE (it goes with its cue)."""
    slot = _find_slot(request, backend, slot_id)
    if slot["source"] != SOURCE_CAPTURE:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "NOT_A_CAPTURE",
                "message": "only captures are deleted here; a loop cue slot goes with its cue",
            },
        )
    sampler_store.delete_capture(
        state_db_path(request), slot["stable_id"], in_ms=slot["in_ms"], out_ms=slot["out_ms"], slot_id=slot_id
    )
    _published(slot["stable_id"])
    return Response(status_code=204)


@router.get("/sets")
def list_sets(request: Request) -> dict[str, Any]:
    """Every stored set (name, description, slot layout), by id."""
    return {"sets": sampler_store.read_sets(state_db_path(request))}


@router.put("/sets/{set_id}")
def put_set(set_id: str, body: SetPut, request: Request) -> dict[str, Any]:
    """Save one set's name, description and slot layout and return it."""
    return sampler_store.write_set(
        state_db_path(request),
        set_id,
        name=body.name,
        description=body.description,
        slot_ids=body.slot_ids,
        updated_at=canonical_now(),
    )


@router.get("/pins")
def list_pins(request: Request) -> dict[str, Any]:
    """Pinned slot ids, oldest pin first."""
    return {"pins": sampler_store.read_pins(state_db_path(request))}


@router.put("/pins/{slot_id}")
def put_pin(slot_id: str, body: PinPut, request: Request, backend: ReadState) -> dict[str, Any]:
    """Pin needs a live slot (404 otherwise); unpin always succeeds so a stale pin can be cleared."""
    if body.pinned:
        _find_slot(request, backend, slot_id)
    pins = sampler_store.write_pin(state_db_path(request), slot_id, pinned=body.pinned, pinned_at=canonical_now())
    return {"pins": pins}


# ``:path``: a classic file pad's id carries an encoded path, and a test client or proxy that decodes %2F twice
# would otherwise turn it into extra path segments; the OpenAPI path stays ``/clips/{slot_id}``.
@router.get(
    "/clips/{slot_id:path}",
    response_class=Response,
    responses={200: {"content": {"audio/flac": {}}, "description": "The clip, region plus X-Guard-Ms each side"}},
)
def get_clip(slot_id: str, request: Request, backend: ReadState, part: ClipPart | None = None) -> Response:
    """The slot's region plus guard as audio/flac (``?part`` from its stem-set), rendered on first ask."""
    # A classic sampler pad: its whole bank slot source, checked against the banks document (#9987).
    # Before the ``.flac`` strip: a file pad's source key may itself end in ``.flac``.
    if is_classic(slot_id):
        return serve_clip(classic_clip(request, slot_id, part))
    slot_id = slot_id.removesuffix(".flac")
    # Hit path: a cached clip was rendered for a slot that was validated then, so serve it without
    # building the catalogue (the edges warm 40 clips at once).
    hit = cached_clip(request, slot_id, part)
    if hit is not None:
        with suppress(FileNotFoundError):  # another request's LRU sweep evicted it after the check: render again
            return serve_clip(hit)
    return serve_clip(rendered_clip(request, _find_slot(request, backend, slot_id), part, background=False))


# ---------- classic sampler banks (#9994) ------------------------------------------------


def _banks_view(request: Request, backend: StateBackend, doc: dict[str, Any]) -> dict[str, Any]:
    """The document with every loaded slot marked ``missing``: a ``stable_id`` the library no longer resolves."""
    slots = [slot for bank in doc["banks"] for slot in bank if slot is not None]
    stable_ids = sorted({s["stable_id"] for s in slots if "stable_id" in s})
    live = sources_for(request).meta(request, backend, stable_ids) if stable_ids else {}
    banks = [
        [None if s is None else {**s, "missing": "stable_id" in s and s["stable_id"] not in live} for s in bank]
        for bank in doc["banks"]
    ]
    return {**doc, "banks": banks}


@router.get("/banks", response_model=BanksView, response_model_exclude_none=True)
def get_banks(request: Request, backend: ReadState) -> dict[str, Any]:
    """The classic sampler banks (4 x 16 empty when never put); a gone track's slot is kept, ``missing`` true."""
    return _banks_view(request, backend, sampler_banks.read_banks(state_db_path(request)))


@router.put("/banks", response_model=BanksView, response_model_exclude_none=True)
def put_banks(body: BanksPut, request: Request, backend: ReadState) -> dict[str, Any]:
    """Replace the whole banks document atomically (temp file and rename) and answer it as GET would."""
    doc = {
        "v": body.v,
        "active_bank": body.active_bank,
        "master_gain_db": body.master_gain_db,
        "banks": [[None if s is None else s.model_dump(exclude_none=True) for s in bank] for bank in body.banks],
    }
    return _banks_view(request, backend, sampler_banks.write_banks(state_db_path(request), doc))


# ---------- classic sampler captures (#9998) ---------------------------------------------


def _capture_too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail={
            "code": "CAPTURE_TOO_LARGE",
            "message": f"a capture is at most {sampler_captures.MAX_CAPTURE_BYTES} bytes",
        },
    )


async def _capped_body(request: Request) -> bytes:
    """The request body, refused with 413 as soon as it passes the capture cap (never read whole first)."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > sampler_captures.MAX_CAPTURE_BYTES:
            raise _capture_too_large()
    return bytes(body)


@router.post(
    "/captures",
    response_model=CaptureStored,
    responses={413: {"description": "Over the capture size cap"}, 422: {"description": "Not a PCM or float WAV"}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def post_capture(request: Request) -> dict[str, Any]:
    """Store a rendered loop WAV under ``sampler-captures/`` by content hash; answer the slot path and length."""
    data = await _capped_body(request)
    try:
        stored = await run_in_threadpool(sampler_captures.store_capture, state_db_path(request), data)
    except sampler_captures.CaptureTooLarge as error:
        raise _capture_too_large() from error
    except sampler_captures.InvalidCapture as error:
        raise HTTPException(status_code=422, detail={"code": "CAPTURE_NOT_WAV", "message": str(error)}) from error
    return {"path": str(stored.path), "duration_ms": stored.duration_ms}
