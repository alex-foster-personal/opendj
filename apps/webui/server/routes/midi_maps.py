"""Installed controller device maps (in-app MIDI device onboarding).

GET    /api/v1/midi/maps            - installed maps, summary rows
GET    /api/v1/midi/maps/{map_id}   - one full document
PUT    /api/v1/midi/maps/{map_id}   - install or replace, validated
DELETE /api/v1/midi/maps/{map_id}   - uninstall
GET    /api/v1/midi/catalog?q=      - search the offline device store

The document is the SAME shape the browser registers at runtime (camelCase,
``DeviceMap`` in apps/webui/frontend/src/lib/rb/midi/midi-types.ts) plus a
required per-binding ``provenance``. Keeping the wire shape identical means the
frontend registers a fetched document with no key translation, and validating
the whole action union HERE means a map that the runtime could not accept is
refused at the edge rather than surviving to the browser. Design:
specs/controller-onboarding.md sections 3.1-3.2.

The JSON files under ``<data>/state/controller-maps/`` are the source of truth.
There is deliberately NO sqlite index: listing globs a directory of tens of
files, the tier/shadowing the index was going to power is computed in the
frontend registry (webmidi.svelte.ts ``listDeviceMaps``), and a second store
would only add a way for the two to disagree.

Feedback output (``leds``, ``meters``, ``init``) is not carried yet; bindings
first. Adding them is an additive optional field, not a schema bump.

Errors, by code:
  404 MIDI_MAP_NOT_FOUND         no installed map with that id
  422 MIDI_MAP_INVALID           document rejected; message names the binding
  422 MIDI_MAP_ID_MISMATCH       body id does not match the path id
  503 MIDI_CATALOG_UNAVAILABLE   the offline device store is not on disk
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from apps.shared.events import publish
from apps.shared.paths import DATA_DIR
from apps.shared.platform_paths import PROJECT_ROOT
from apps.webui.server.backend import StateBackend
from apps.webui.server.deps import get_write_state

WriteGate = Annotated[StateBackend, Depends(get_write_state)]

router = APIRouter(prefix="/midi", tags=["midi"])

_MAPS_SUBDIR = "controller-maps"

# The offline device store: 107 controllers of wire data harvested from vendor
# PDFs and the Mixxx mapping set. Repo content, not user data, so it is rooted
# at the checkout rather than the data dir.
_CATALOG_DIR = PROJECT_ROOT / "tools" / "deck-diagrams"

# A map id is also its filename, so it is a strict slug: no separators, no
# traversal, no case ambiguity on a case-insensitive filesystem.
_MAP_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

DeckId = Literal[1, 2, 3, 4]
HotCueSlot = Literal["A", "B", "C", "D", "E", "F", "G", "H"]
EqBand = Literal["low", "mid", "high"]


# ----- pydantic models (inline per router convention, see progress.py) -------


class _Strict(BaseModel):
    """Shared base for wire-input models: an unknown field 422s instead of
    being silently dropped. Subclass model_config merges with this one
    (pydantic v2), so a subclass keeps extra='forbid' while adding its own."""

    model_config = ConfigDict(extra="forbid")


class Provenance(_Strict):
    """Where one binding's wire numbers came from. Required on every binding:
    it is what makes the no-fabricated-numbers house rule mechanical rather
    than a comment convention, and it drives the confidence badge in the UI."""

    model_config = ConfigDict(frozen=True)

    tier: Literal["vendor-pdf", "mixxx", "learned", "hand"]
    cite: str = Field(min_length=1)
    # Tier-2 (community) imports start unverified until a human or a hardware
    # pass confirms them. See DATA-SOURCES-INDEX.md.
    verified: bool = False


class MidiSource(_Strict):
    model_config = ConfigDict(frozen=True)

    ch: int = Field(ge=1, le=16)
    kind: Literal["note", "cc", "pitchbend"]
    id: int = Field(ge=0, le=127)

    @model_validator(mode="after")
    def _pitchbend_carries_no_id(self) -> MidiSource:
        if self.kind == "pitchbend" and self.id != 0:
            raise ValueError("pitchbend carries no id byte, so id must be 0")
        return self


class DeckPlayToggle(_Strict):
    type: Literal["deck_play_toggle"]
    deck: DeckId


class DeckCue(_Strict):
    type: Literal["deck_cue"]
    deck: DeckId


class DeckHotCue(_Strict):
    type: Literal["deck_hot_cue"]
    deck: DeckId
    slot: HotCueSlot


class DeckBeatLoop(_Strict):
    type: Literal["deck_beat_loop"]
    deck: DeckId
    # Fractional loops interpolate adjacent measured PQTZ timestamps.
    beats: float = Field(gt=0, allow_inf_nan=False)


class DeckAutoLoopToggle(_Strict):
    type: Literal["deck_auto_loop_toggle"]
    deck: DeckId


class DeckLoopExit(_Strict):
    type: Literal["deck_loop_exit"]
    deck: DeckId


class DeckStemEqToggle(_Strict):
    type: Literal["deck_stem_eq_toggle"]
    deck: DeckId


class MixerChannel(_Strict):
    type: Literal["mixer_channel"]
    deck: DeckId
    target: Literal["trim", "eq", "fader"]
    band: EqBand | None = None

    @model_validator(mode="after")
    def _band_belongs_to_eq_only(self) -> MixerChannel:
        if self.target == "eq" and self.band is None:
            raise ValueError("mixer_channel target 'eq' requires a band")
        if self.target != "eq" and self.band is not None:
            raise ValueError(f"mixer_channel target '{self.target}' must not carry a band")
        return self


class MixerGlobal(_Strict):
    type: Literal["mixer_global"]
    target: Literal["crossfader", "master"]


class ChannelCue(_Strict):
    type: Literal["channel_cue"]
    deck: DeckId


class DeckPitch(_Strict):
    type: Literal["deck_pitch"]
    deck: DeckId
    # +32 is the MIDI-standard MSB/LSB pairing; null is a plain 7-bit fader.
    lsb_offset: int | None = Field(alias="lsbOffset", ge=0, le=127)

    model_config = ConfigDict(populate_by_name=True)


class BrowseEncoder(_Strict):
    type: Literal["browse_encoder"]


class BrowseLoad(_Strict):
    type: Literal["browse_load"]
    deck: DeckId


class ShiftModifier(_Strict):
    type: Literal["shift_modifier"]


MidiActionModel = Annotated[
    DeckPlayToggle
    | DeckCue
    | DeckHotCue
    | DeckBeatLoop
    | DeckAutoLoopToggle
    | DeckLoopExit
    | DeckStemEqToggle
    | MixerChannel
    | MixerGlobal
    | ChannelCue
    | DeckPitch
    | BrowseEncoder
    | BrowseLoad
    | ShiftModifier,
    Field(discriminator="type"),
]


# webmidi.svelte.ts _valueFor() only returns kind:'button' for src.kind ===
# 'note'; action-glue.svelte.ts's _pressed() throws on anything else.
_BUTTON_ACTIONS = frozenset(
    {"deck_play_toggle", "deck_cue", "deck_hot_cue", "deck_beat_loop", "deck_loop_exit",
     "channel_cue", "browse_load", "shift_modifier", "deck_stem_eq_toggle",
     "deck_auto_loop_toggle"}
)

# _continuous01() throws on anything but kind:'continuous'/'continuous14'. A
# cc source with relative=true instead produces kind:'relative' (_valueFor),
# so that combination is rejected below; pitchbend ignores `relative`
# entirely (dispatch always builds continuous14 for it), so it is not.
_CONTINUOUS_ACTIONS = frozenset({"mixer_channel", "mixer_global"})


class Binding(_Strict):
    model_config = ConfigDict(populate_by_name=True)

    source: MidiSource
    action: MidiActionModel
    shift: bool = False
    invert: bool = False
    relative: bool = False
    provenance: Provenance

    @model_validator(mode="after")
    def _source_matches_action(self) -> Binding:
        """Reject a source/action combo the runtime cannot register, mirroring
        webmidi.svelte.ts/_buildIndex and action-glue.svelte.ts's
        _pressed()/_continuous01() throws exactly."""
        kind = self.source.kind
        action = self.action
        if isinstance(action, DeckPitch):
            if kind not in ("cc", "pitchbend"):
                raise ValueError(f"deck_pitch must bind a cc or pitchbend source, got {kind!r}")
            if action.lsb_offset is not None and kind != "cc":
                raise ValueError("deck_pitch 14-bit lsbOffset requires a cc source")
            if kind == "cc" and action.lsb_offset is None and self.relative:
                raise ValueError("deck_pitch cannot be bound relative without lsbOffset")
            if kind == "cc" and action.lsb_offset is not None:
                # webmidi.svelte.ts's _buildIndex() keys the LSB index at
                # source.id + lsbOffset: offset 0 collides with the MSB's own
                # key, so _dispatch() reads every MSB CC as an LSB and drops
                # it as "arrived before any MSB"; an offset that pushes the
                # sum past 127 indexes a CC number that cannot exist.
                if action.lsb_offset == 0:
                    raise ValueError(
                        "deck_pitch lsbOffset must be > 0: 0 makes the LSB key collide with "
                        "the MSB's own source id, so every message is dropped as "
                        "'LSB arrived before any MSB'"
                    )
                lsb_cc = self.source.id + action.lsb_offset
                if lsb_cc > 127:
                    raise ValueError(
                        f"deck_pitch lsbOffset {action.lsb_offset} plus source id "
                        f"{self.source.id} indexes nonexistent CC {lsb_cc} (max 127)"
                    )
            return self
        if isinstance(action, BrowseEncoder):
            if kind != "cc":
                raise ValueError(f"browse_encoder must bind a cc source, got {kind!r}")
            if not self.relative:
                raise ValueError("browse_encoder must be bound relative")
            return self
        if action.type in _BUTTON_ACTIONS:
            if kind != "note":
                raise ValueError(f"{action.type} must bind a note source, got {kind!r}")
            return self
        if action.type in _CONTINUOUS_ACTIONS:
            if kind not in ("cc", "pitchbend"):
                raise ValueError(f"{action.type} must bind cc or pitchbend, got {kind!r}")
            if kind == "cc" and self.relative:
                raise ValueError(f"{action.type} cannot be bound relative")
            return self
        raise AssertionError(f"unhandled action type in binding validator: {action.type!r}")

    @model_validator(mode="after")
    def _shift_modifier_not_itself_shifted(self) -> Binding:
        # The runtime starts with shiftHeld false, so a shift_modifier bound
        # shift:true can never receive its own first press - its whole shifted
        # layer would be permanently unreachable.
        if isinstance(self.action, ShiftModifier) and self.shift:
            raise ValueError(
                "shift_modifier cannot itself be bound shift:true - the runtime "
                "starts with shiftHeld false, so a shifted shift_modifier can "
                "never receive its first press and its whole shifted layer is "
                "unreachable"
            )
        return self


# webmidi.svelte.ts _resolveMap() runs `new RegExp(map.nameMatch, 'i')`, so a
# pattern that only compiles under Python's re would break at match time in
# the browser. Deny-list rather than shelling out to node: the packaged
# daemon must not gain a hard runtime dependency on Node just to validate one
# field.
_NAME_MATCH_UNSUPPORTED_IN_JS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\(\?P[<=]"), "named groups (?P<name>...) / (?P=name) are Python-only"),
    (re.compile(r"\(\?#"), "comment groups (?#...) are Python-only"),
    (re.compile(r"\(\?[aiLmsux]+\)"), "inline flags like (?i) are Python-only"),
    (re.compile(r"\\[AZ]"), r"\A / \Z anchors are Python-only, use ^ / $"),
)


class MidiMapDoc(_Strict):
    """One controller, as installed. camelCase on the wire to match DeviceMap."""

    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schemaVersion", default=1)
    id: str
    vendor: str = Field(min_length=1)
    model: str = Field(min_length=1)
    name_match: str = Field(alias="nameMatch", min_length=1)
    bindings: list[Binding] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _id_is_a_slug(cls, value: str) -> str:
        if _MAP_ID.fullmatch(value) is None:
            raise ValueError(f"id must be a lowercase slug, got {value!r}")
        return value

    @field_validator("name_match")
    @classmethod
    def _name_match_compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"nameMatch is not a valid regex: {exc}") from exc
        for pattern, reason in _NAME_MATCH_UNSUPPORTED_IN_JS:
            if pattern.search(value):
                raise ValueError(f"nameMatch uses syntax the browser cannot run: {reason}")
        return value

    @model_validator(mode="after")
    def _no_duplicate_sources(self) -> MidiMapDoc:
        seen: dict[tuple[bool, int, str, int], int] = {}
        # webmidi.svelte.ts's lsbIndex is shift-independent (keyed by chKey(ch, cc)
        # alone) and _dispatch() checks it BEFORE the shift-aware index, so an
        # ordinary binding on a CC claimed as an LSB slot - shifted or not - can
        # never fire, and two deck_pitch bindings cannot claim the same LSB slot.
        lsb_claims: dict[tuple[int, int], int] = {}
        for at, binding in enumerate(self.bindings):
            key = (binding.shift, binding.source.ch, binding.source.kind, binding.source.id)
            if key in seen:
                raise ValueError(
                    f"bindings[{at}]: duplicate source, already bound by bindings[{seen[key]}]"
                )
            seen[key] = at
            action = binding.action
            if (
                isinstance(action, DeckPitch)
                and binding.source.kind == "cc"
                and action.lsb_offset is not None
            ):
                lsb_key = (binding.source.ch, binding.source.id + action.lsb_offset)
                if lsb_key in lsb_claims:
                    raise ValueError(
                        f"bindings[{at}]: 14-bit LSB at ch{lsb_key[0]} cc{lsb_key[1]} collides "
                        f"with bindings[{lsb_claims[lsb_key]}]'s LSB - webmidi.svelte.ts's "
                        "lsbIndex is shift-independent and keyed by (ch, cc) alone"
                    )
                lsb_claims[lsb_key] = at
        for at, binding in enumerate(self.bindings):
            if binding.source.kind != "cc":
                continue
            lsb_key = (binding.source.ch, binding.source.id)
            claimant = lsb_claims.get(lsb_key)
            if claimant is not None and claimant != at:
                raise ValueError(
                    f"bindings[{at}]: source ch{lsb_key[0]} cc{lsb_key[1]} is shadowed by "
                    f"bindings[{claimant}]'s 14-bit LSB - webmidi.svelte.ts's dispatch() checks "
                    "lsbIndex before the shift-aware binding index, so this control can never fire"
                )
        return self


class MidiMapSummary(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str
    vendor: str
    model: str
    name_match: str = Field(serialization_alias="nameMatch")
    binding_count: int = Field(serialization_alias="bindingCount")
    provenance: dict[str, int]


class MidiMapListOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    maps: list[MidiMapSummary]


# How much a catalog device's numbers can be trusted. This grade is the whole
# point of the catalog surface: 'bootstrap-stub' rows hold sequential INVENTED
# codes read off a plate photo, so importing one would put fabricated wire data
# into the registry. Their vendor PDF IS archived under devices/<id>/source/ -
# they are "never table-parsed", not "no data exists".
CatalogQuality = Literal["vendor-pdf", "mixxx", "bootstrap-stub"]


class CatalogDevice(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str
    brand: str
    model: str
    quality: CatalogQuality
    control_count: int = Field(serialization_alias="controlCount")
    has_layout: bool = Field(serialization_alias="hasLayout")
    popularity_rank: int | None = Field(default=None, serialization_alias="popularityRank")


class CatalogOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    devices: list[CatalogDevice]
    # Grade totals for the WHOLE store, not the filtered page: a coverage
    # figure quoted off a search result is a lie waiting to be repeated.
    counts: dict[str, int]


# ----- _helpers -------------------------------------------------------------


def _maps_dir(request: Request) -> Path:
    configured = getattr(request.app.state, "data_dir", None)
    root = Path(configured) if configured is not None else DATA_DIR
    return root / "state" / _MAPS_SUBDIR


def _invalid(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "MIDI_MAP_INVALID", "message": message})


def _not_found(map_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "MIDI_MAP_NOT_FOUND", "message": f"no installed map with id {map_id!r}"},
    )


def _require_slug(map_id: str) -> str:
    if _MAP_ID.fullmatch(map_id) is None:
        raise _invalid(f"map id must be a lowercase slug, got {map_id!r}")
    return map_id


def _describe(exc: ValidationError) -> str:
    """Turn pydantic's loc tuple into 'bindings[1].action: ...' so the caller
    is told WHICH binding is wrong, not just that something is."""
    first = exc.errors()[0]
    parts: list[str] = []
    for piece in first["loc"]:
        if isinstance(piece, int):
            parts[-1] = f"{parts[-1]}[{piece}]" if parts else f"[{piece}]"
        else:
            parts.append(str(piece))
    where = ".".join(parts)
    return f"{where}: {first['msg']}" if where else first["msg"]


def _parse(raw: Any, *, whose: str) -> MidiMapDoc:
    try:
        return MidiMapDoc.model_validate(raw)
    except ValidationError as exc:
        raise _invalid(f"{whose}: {_describe(exc)}") from exc


def _read(path: Path) -> MidiMapDoc:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _invalid(f"{path.stem}: file is not valid JSON ({exc})") from exc
    return _parse(raw, whose=path.stem)


def _write_atomic(path: Path, doc: MidiMapDoc) -> None:
    """Whole-document replace with no half-written window: a map read back
    mid-write would register a partial controller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc.model_dump(by_alias=True), fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _catalog_quality(midi: dict[str, Any]) -> CatalogQuality:
    if midi.get("bootstrap") is True:
        return "bootstrap-stub"
    source = str(midi.get("source", ""))
    if source.startswith("mixxx:") or midi.get("mixxx_gpl") is True:
        return "mixxx"
    if source.endswith(".pdf"):
        return "vendor-pdf"
    # An unrecognised provenance is graded down, never up. Guessing 'vendor-pdf'
    # for a file we cannot place is exactly how fabricated numbers get trusted.
    return "bootstrap-stub"


# Grading the store means opening 107 files, which is too much to repeat on
# every keystroke of a search box. Memoised on the devices dir mtime, so a
# rebuilt catalog invalidates it without a daemon restart.
_catalog_cache: dict[tuple[str, int], list[CatalogDevice]] = {}


def _catalog_index() -> list[CatalogDevice]:
    devices_dir = _CATALOG_DIR / "devices"
    if not devices_dir.is_dir():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MIDI_CATALOG_UNAVAILABLE",
                "message": f"offline device store not found at {devices_dir}",
            },
        )
    key = (str(devices_dir), devices_dir.stat().st_mtime_ns)
    cached = _catalog_cache.get(key)
    if cached is not None:
        return cached

    named: dict[str, dict[str, Any]] = {}
    catalog_file = _CATALOG_DIR / "catalog" / "controllers.json"
    if catalog_file.is_file():
        for row in json.loads(catalog_file.read_text(encoding="utf-8")).get("controllers", []):
            named[str(row.get("id"))] = row

    devices: list[CatalogDevice] = []
    for midi_path in sorted(devices_dir.glob("*/midi.json")):
        device_id = midi_path.parent.name
        midi = json.loads(midi_path.read_text(encoding="utf-8"))
        row = named.get(device_id, {})
        devices.append(
            CatalogDevice(
                id=device_id,
                brand=str(row.get("brand") or midi.get("vendor") or "unknown"),
                model=str(row.get("model") or midi.get("device") or device_id),
                quality=_catalog_quality(midi),
                control_count=len(midi.get("controls", [])),
                has_layout=(midi_path.parent / "layout.json").is_file(),
                popularity_rank=row.get("popularity_rank"),
            )
        )
    _catalog_cache.clear()
    _catalog_cache[key] = devices
    return devices


def _installed_name_match_clash(request: Request, *, name_match: str, exclude_id: str) -> str | None:
    """Id of another installed map already using this exact nameMatch, or
    None. _resolveMap() picks the first regex match among registered maps in
    order, so two installed maps sharing a pattern make port resolution
    silently order-dependent. Exact string equality only - detecting genuine
    overlap between two different regexes is a separate problem."""
    maps_dir = _maps_dir(request)
    if not maps_dir.is_dir():
        return None
    for path in sorted(maps_dir.glob("*.json")):
        if path.stem == exclude_id:
            continue
        if _read(path).name_match == name_match:
            return path.stem
    return None


def _summarize(doc: MidiMapDoc) -> MidiMapSummary:
    tiers: dict[str, int] = {}
    for binding in doc.bindings:
        tiers[binding.provenance.tier] = tiers.get(binding.provenance.tier, 0) + 1
    return MidiMapSummary(
        id=doc.id,
        vendor=doc.vendor,
        model=doc.model,
        name_match=doc.name_match,
        binding_count=len(doc.bindings),
        provenance=tiers,
    )


# ----- routes ---------------------------------------------------------------


@router.get("/maps", response_model=MidiMapListOut, response_model_by_alias=True)
def list_midi_maps(request: Request) -> MidiMapListOut:
    maps_dir = _maps_dir(request)
    if not maps_dir.is_dir():
        return MidiMapListOut(maps=[])
    # A file that will not parse is surfaced, never skipped: silently hiding a
    # broken map is how a controller "just stops working" with no explanation.
    return MidiMapListOut(maps=[_summarize(_read(p)) for p in sorted(maps_dir.glob("*.json"))])


@router.get("/catalog", response_model=CatalogOut, response_model_by_alias=True)
def search_midi_catalog(q: str = "") -> CatalogOut:
    devices = _catalog_index()
    counts: dict[str, int] = {}
    for device in devices:
        counts[device.quality] = counts.get(device.quality, 0) + 1
    needle = q.strip().lower()
    if needle:
        devices = [
            d
            for d in devices
            if needle in d.id.lower() or needle in d.brand.lower() or needle in d.model.lower()
        ]
    return CatalogOut(devices=devices, counts=counts)


@router.get("/maps/{map_id}", response_model=MidiMapDoc, response_model_by_alias=True)
def get_midi_map(map_id: str, request: Request) -> MidiMapDoc:
    path = _maps_dir(request) / f"{_require_slug(map_id)}.json"
    if not path.is_file():
        raise _not_found(map_id)
    return _read(path)


@router.put("/maps/{map_id}", response_model=MidiMapDoc, response_model_by_alias=True)
def put_midi_map(
    map_id: str,
    body: dict[str, Any],
    request: Request,
    _backend: WriteGate,
) -> MidiMapDoc:
    _require_slug(map_id)
    doc = _parse(body, whose="document")
    if doc.id != map_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MIDI_MAP_ID_MISMATCH",
                "message": f"body id {doc.id!r} does not match path id {map_id!r}",
            },
        )
    clash = _installed_name_match_clash(request, name_match=doc.name_match, exclude_id=map_id)
    if clash is not None:
        raise _invalid(f"nameMatch {doc.name_match!r} is already used by installed map {clash!r}")
    _write_atomic(_maps_dir(request) / f"{map_id}.json", doc)
    publish("library.changed", {"kind": "midi_maps", "ids": [map_id]})
    return doc


@router.delete("/maps/{map_id}", status_code=204)
def delete_midi_map(
    map_id: str,
    request: Request,
    _backend: WriteGate,
) -> Response:
    path = _maps_dir(request) / f"{_require_slug(map_id)}.json"
    if not path.is_file():
        raise _not_found(map_id)
    path.unlink()
    publish("library.changed", {"kind": "midi_maps", "ids": [map_id]})
    return Response(status_code=204)
