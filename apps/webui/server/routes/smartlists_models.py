"""Pydantic models for the smartlists router."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import TrackRowOut


class SmartlistSummary(BaseModel):
    """List/detail row for one smartlist (contract for rule-editor-ui)."""

    id: str
    name: str
    rule: dict[str, Any]
    rule_summary: str
    order_by: str
    referenced_fields: list[str]
    rule_schema_version: int
    last_evaluated_at: str | None
    created_at: str
    modified_at: str
    count: int | None = None


class SmartlistCreateIn(BaseModel):
    """Create payload matching ``python -m apps.smartlists.cli.create``."""

    name: str = Field(min_length=1, description="Display name (non-empty)")
    rule: dict[str, Any]
    order_by: str | None = Field(
        None,
        description="Sort key; defaults to 'added_date desc' like the CLI",
    )


class SmartlistUpdateIn(BaseModel):
    """Complete desired rule plus optional replacement ordering and name."""

    rule: dict[str, Any]
    order_by: str | None = None
    name: str | None = Field(None, min_length=1)


class SmartlistDuplicateIn(BaseModel):
    """Optional name override for a duplicated smartlist."""

    name: str | None = Field(None, min_length=1)


class SmartlistConflictBody(BaseModel):
    """Structured stale-write response with current state and fresh ETag."""

    error: Literal["conflict"] = "conflict"
    message: str
    current: SmartlistSummary
    etag: str


class SmartlistPreconditionRequiredBody(BaseModel):
    """Structured response when an update omits its CAS precondition."""

    error: Literal["precondition_required"] = "precondition_required"
    message: str
    details: None = None


class SmartlistTracks(BaseModel):
    """Live evaluation result, shaped like playlist detail."""

    smartlist_id: str
    name: str
    rule_summary: str
    order_by: str
    items: list[str]
    tracks: list[TrackRowOut]


__all__ = [
    "SmartlistConflictBody",
    "SmartlistCreateIn",
    "SmartlistDuplicateIn",
    "SmartlistPreconditionRequiredBody",
    "SmartlistSummary",
    "SmartlistTracks",
    "SmartlistUpdateIn",
]
