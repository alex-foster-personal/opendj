"""Delivery surface of ``apps.opendj_pins``: mount helper and shared pin types.

Delivery layers (``apps.webui``) and tests import this module or
``apps.opendj_pins.contract``; nothing else outside the package may.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Request

from .merge import keep_unknown_fields
from .models import (
    AttachmentOut,
    BuildStampOut,
    CommentArchiveOut,
    CommentCreateIn,
    CommentListOut,
    CommentOut,
    CommentReplyIn,
    CommentReplyOut,
    CommentUpdateIn,
    PinEnvironmentOut,
)
from .placement import (
    MAX_NEARBY_ANCHORS,
    PinElementOffset,
    PinNearbyAnchor,
    require_anchor_for_offset,
)
from .replies import append_agent_note_to_replies, seed_replies_from_agent_note
from .router import router as pins_router
from .store import PinsStore
from .ui_config import PIN_UI_CONFIG_MAX_SWITCHES, PinUiConfig

BuildStampFactory = Callable[[Request], BuildStampOut]
EnvironmentFactory = Callable[[CommentCreateIn, Request], PinEnvironmentOut | None]


def mount_pins_router(
    app: FastAPI,
    *,
    sqlite_path: Path,
    url_prefix: str = "",
    attachments_dir: Path | None = None,
    build_stamp: BuildStampFactory | None = None,
    pin_environment: EnvironmentFactory | None = None,
) -> PinsStore:
    """Mount comment-pins routes on ``app`` and return the SQLite store."""

    prefix = url_prefix.strip("/")
    attachment_url_prefix = f"/{prefix}/feedback/attachments".replace("//", "/") if prefix else "/feedback/attachments"
    store = PinsStore(
        sqlite_path,
        attachments_dir=attachments_dir or sqlite_path.parent / "pin-attachments",
        attachment_url_prefix=attachment_url_prefix,
    )
    app.state.opendj_pins_store = store
    if build_stamp is not None:
        app.state.opendj_pins_build_stamp = build_stamp
    if pin_environment is not None:
        app.state.opendj_pins_environment = pin_environment
    mount_at = f"/{prefix}".rstrip("/") if prefix else ""
    app.include_router(pins_router, prefix=mount_at)
    return store


__all__ = [
    "MAX_NEARBY_ANCHORS",
    "PIN_UI_CONFIG_MAX_SWITCHES",
    "AttachmentOut",
    "BuildStampFactory",
    "BuildStampOut",
    "CommentArchiveOut",
    "CommentCreateIn",
    "CommentListOut",
    "CommentOut",
    "CommentReplyIn",
    "CommentReplyOut",
    "CommentUpdateIn",
    "EnvironmentFactory",
    "PinElementOffset",
    "PinEnvironmentOut",
    "PinNearbyAnchor",
    "PinUiConfig",
    "PinsStore",
    "append_agent_note_to_replies",
    "keep_unknown_fields",
    "mount_pins_router",
    "pins_router",
    "require_anchor_for_offset",
    "seed_replies_from_agent_note",
]
