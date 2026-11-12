"""Wire models for mountable comment-pins HTTP (OpenAPI feedback tag)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apps.opendj_pins.placement import (
    MAX_NEARBY_ANCHORS,
    PinElementOffset,
    PinNearbyAnchor,
    require_anchor_for_offset,
)
from apps.opendj_pins.ui_config import PinUiConfig


class BuildStampOut(BaseModel):
    """Provenance of the build that recorded a piece of feedback.

    Exactly one of the two shapes: a resolved (git_sha, built_at_utc, source)
    triple, or an explicit error naming why resolution failed. Never blank.
    """

    model_config = ConfigDict(frozen=True)

    git_sha: str | None = None
    built_at_utc: str | None = None
    source: str | None = None
    error: str | None = None


class AttachmentOut(BaseModel):
    """A screenshot pasted into a comment pin (issue #1333, part 2 of #928).

    ``url`` is the GET route that streams the stored bytes back (relative,
    same convention as ``issue_url`` on ``CommentOut``); ``content_type``,
    ``size_bytes``, and ``content_hash`` are recorded once, at upload time, so
    a caller can decide whether to fetch the bytes without a HEAD round trip
    first. ``content_hash`` is a lowercase hex SHA-256 of the stored bytes for
    a later asset-tier push; it is absent on attachments uploaded before issue
    #11234. The bytes themselves live under
    ``<data-dir>/feedback/attachments/`` - see ``feedback_attachments.py``, the
    module that actually writes and serves them; this file only owns the small
    record ``comments.json`` carries.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    content_type: str
    size_bytes: int
    url: str
    content_hash: str | None = None


class CommentReplyOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    author: Literal["operator", "agent"]
    text: str
    created_at: str
    agent_kind: str | None = None


class CommentReplyIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    author: Literal["operator", "agent"] = "operator"
    agent_kind: str | None = None


class PinEnvironmentOut(BaseModel):
    """Runtime facts needed to reproduce a pinned UI defect.

    ``machine`` and ``release_version`` are already exposed by the running
    daemon's settings/health surfaces. The browser contributes its UI kind,
    viewport dimensions and a closed ``ui_config`` snapshot (see
    ``PinUiConfig``): no user agent, URL query, file path or track title.

    ``user_email`` is the one personal field (pin 49f9d217). The daemon stamps
    it from the session cookie, the same identity ``GET /api/v1/auth/me``
    already returns to this browser; a request body cannot set it. It is null
    when nobody is signed in, and absent on pins older than this field.
    """

    model_config = ConfigDict(frozen=True)

    ui: Literal["chrome-loop", "packaged-app"]
    viewport_width: int
    viewport_height: int
    machine: str
    release_version: str
    user_email: str | None = None
    ui_config: PinUiConfig | None = None


class CommentOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    x_pct: float
    y_pct: float
    anchor: str | None
    page: str
    text: str
    created_at: str
    build: BuildStampOut
    environment: PinEnvironmentOut | None = None
    status: str | None = None
    issue_url: str | None = None
    agent_note: str | None = None
    updated_at: str | None = None
    fixed_in_sha: str | None = None
    fixed_at: str | None = None
    harvested_at: str | None = None
    agent_snapshot_at: str | None = None
    author: Literal["operator", "agent"] = Field(default_factory=lambda: "operator")
    agent_kind: str | None = None
    attachment: AttachmentOut | None = None
    replies: list[CommentReplyOut] = Field(default_factory=list)
    element_offset: PinElementOffset | None = None
    nearby_anchors: list[PinNearbyAnchor] = Field(default_factory=list)


class CommentListOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    comments: list[CommentOut]


class CommentCreateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    x_pct: float = Field(ge=0, le=100)
    y_pct: float = Field(ge=0, le=100)
    anchor: str | None = None
    page: str = Field(min_length=1)
    text: str = Field(min_length=1)
    ui: Literal["chrome-loop", "packaged-app"]
    viewport_width: int = Field(ge=1, le=100_000)
    viewport_height: int = Field(ge=1, le=100_000)
    author: Literal["operator", "agent"] = Field(default_factory=lambda: "operator")
    agent_kind: str | None = None
    ui_config: PinUiConfig | None = None
    element_offset: PinElementOffset | None = None
    nearby_anchors: list[PinNearbyAnchor] = Field(default_factory=list, max_length=MAX_NEARBY_ANCHORS)

    @model_validator(mode="after")
    def _offset_has_anchor(self) -> CommentCreateIn:
        require_anchor_for_offset(self.anchor, self.element_offset)
        return self


_PATCHABLE_STATUS_PATTERN = "^(open|issued|blocked|fixed|merged)$"


class CommentUpdateIn(BaseModel):
    text: str = Field(default=None, min_length=1)
    status: str | None = Field(default=None, pattern=_PATCHABLE_STATUS_PATTERN)
    issue_url: str | None = None
    agent_note: str | None = None
    fixed_in_sha: str | None = Field(default=None, min_length=7, max_length=40)


class CommentArchiveOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    archived_to: str
    comment: CommentOut
