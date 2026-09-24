"""Settings AI helpers (search synonyms + allowlisted apply proposals).

POST /api/v1/settings/ai-search
POST /api/v1/settings/ai-apply

Does not alter GET /api/v1/settings (daemon dump stays intact).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from apps.webui.server.settings_xai import chat_json, load_xai_config

router = APIRouter(prefix="/settings", tags=["settings-ai"])

AllowedValue = bool | str

ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "theme",
        "hide_broken_links",
        "library_density",
        "beat_sync_max",
        "next_only_filter",
        "remixes_filter",
        "vocals_filter",
        "available_offline_filter",
        "midi_enabled",
        "hide_todo_settings",
        "jog_radial_waveform",
        "auto_sync.rekordbox",
        "auto_sync.djay",
        "auto_sync.open_dj",
        "confirm.delete_playlist",
        "confirm.dblclick_load_play",
    }
)

_ENUM_VALUES: dict[str, frozenset[str]] = {
    "theme": frozenset({"dark", "light"}),
    "library_density": frozenset({"compact", "cosy"}),
}


class AiSearchIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1, max_length=200)
    catalog_ids: list[str] = Field(default_factory=list)


class AiSearchOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    ids: list[str]
    model: str


class AiApplyIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    instruction: str = Field(min_length=1, max_length=400)


class AiApplyProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    value: AllowedValue
    rationale: str


class AiApplyOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    proposal: AiApplyProposal | None = None
    error: str | None = None
    model: str


def _require_xai() -> None:
    try:
        load_xai_config()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "XAI_API_KEY_MISSING", "message": str(exc)},
        ) from exc


def _validate_proposal(key: str, value: Any) -> AllowedValue:
    if key not in ALLOWED_KEYS:
        raise ValueError(f"unknown or disallowed setting key: {key}")
    if key in _ENUM_VALUES:
        if not isinstance(value, str) or value not in _ENUM_VALUES[key]:
            raise ValueError(
                f"{key} must be one of {sorted(_ENUM_VALUES[key])}, got {value!r}"
            )
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false", "on", "off", "1", "0"):
        return value.lower() in ("true", "on", "1")
    raise ValueError(f"{key} expects a boolean, got {value!r}")


@router.post("/ai-search", response_model=AiSearchOut)
def ai_search(body: AiSearchIn) -> AiSearchOut:
    _require_xai()
    allowed = [i for i in body.catalog_ids if isinstance(i, str) and i]
    if not allowed:
        return AiSearchOut(ids=[], model=load_xai_config().model)
    system = (
        "You match user setting-search queries to setting ids. "
        "Return JSON: {\"ids\": [\"...\"]}. Only use ids from the provided catalog. "
        "Prefer intent/synonym matches the keyword filter may miss. Max 12 ids. "
        "No duplicates. If nothing relevant, return {\"ids\": []}."
    )
    user = json_user_search(body.query, allowed)
    try:
        parsed, model = chat_json(system=system, user=user)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "XAI_ERROR", "message": str(exc)},
        ) from exc
    raw_ids = parsed.get("ids") or []
    if not isinstance(raw_ids, list):
        raise HTTPException(
            status_code=502,
            detail={"code": "XAI_BAD_SHAPE", "message": "ids must be a list"},
        )
    allow = set(allowed)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_ids:
        if not isinstance(item, str):
            continue
        if item not in allow or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= 12:
            break
    return AiSearchOut(ids=out, model=model)


@router.post("/ai-apply", response_model=AiApplyOut)
def ai_apply(body: AiApplyIn) -> AiApplyOut:
    _require_xai()
    system = (
        "You propose ONE settings change for a DJ app. "
        "You may only use keys from ALLOWED_KEYS. "
        "Return JSON: {\"key\": \"...\", \"value\": <bool or enum string>, "
        "\"rationale\": \"short\"}. "
        "If the instruction is unclear or needs a disallowed key, "
        "return {\"error\": \"reason\"}."
    )
    user = (
        f"ALLOWED_KEYS: {sorted(ALLOWED_KEYS)}\n"
        f"ENUMS: theme=dark|light; library_density=compact|cosy; "
        f"wheel_sensitivity={{mouse,trackpad}} each in (0.05,4.0]; others=boolean\n"
        f"INSTRUCTION: {body.instruction.strip()}"
    )
    try:
        parsed, model = chat_json(system=system, user=user)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "XAI_ERROR", "message": str(exc)},
        ) from exc

    if isinstance(parsed.get("error"), str) and parsed["error"].strip():
        return AiApplyOut(ok=False, proposal=None, error=parsed["error"].strip(), model=model)

    key = parsed.get("key")
    value = parsed.get("value")
    rationale = parsed.get("rationale") or ""
    if not isinstance(key, str):
        return AiApplyOut(
            ok=False, proposal=None, error="model omitted key", model=model
        )
    try:
        coerced = _validate_proposal(key, value)
    except ValueError as exc:
        return AiApplyOut(ok=False, proposal=None, error=str(exc), model=model)
    return AiApplyOut(
        ok=True,
        proposal=AiApplyProposal(
            key=key,
            value=coerced,
            rationale=str(rationale)[:240],
        ),
        error=None,
        model=model,
    )


def json_user_search(query: str, catalog_ids: list[str]) -> str:
    return (
        f"QUERY: {query.strip()}\n"
        f"CATALOG_IDS ({len(catalog_ids)}): {catalog_ids}"
    )


__all__ = ["ALLOWED_KEYS", "router"]
