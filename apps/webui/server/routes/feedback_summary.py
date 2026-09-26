"""GET /feedback/comments/summary -- operator pin buckets (FB-20, pin 6af63c5e9b7c).

Correlates comment pins with the progress-tree ledger when an issue_url matches
``links.issues`` on a node in ``building`` or ``partial`` status.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from .feedback import _COMMENTS_FILE, _dir, _load
from . import progress as progress_module

router = APIRouter(prefix="/feedback", tags=["feedback"])

_ISSUE_URL_RE = re.compile(r"/issues/(\d+)(?:$|[/?#])")
_PARTIAL_PREFIX = re.compile(r"^partial:", re.I)
_PARTIAL_REF = re.compile(r"(#\d+|https?://\S+)")


class PinOperatorBreakdownOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int = Field(ge=0)
    sent_to_queue: int = Field(ge=0)
    in_progress: int = Field(ge=0)
    delegated: int = Field(ge=0)
    fixed: int = Field(ge=0)
    merged: int = Field(ge=0)
    blocked: int = Field(ge=0)
    harvested: int = Field(ge=0)


class PinStatusSummaryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int = Field(ge=0)
    untriaged: int = Field(ge=0)
    open: int = Field(ge=0)
    issued: int = Field(ge=0)
    blocked: int = Field(ge=0)
    fixed: int = Field(ge=0)
    merged: int = Field(ge=0)
    harvested: int = Field(ge=0)


class CommentSummaryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    operator: PinOperatorBreakdownOut
    lifecycle: PinStatusSummaryOut


def _parse_issue_url(issue_url: str | None) -> int | None:
    if not issue_url:
        return None
    m = _ISSUE_URL_RE.search(issue_url)
    return int(m.group(1)) if m else None


def _parse_progress_issue_ref(raw: str | int) -> int | None:
    if isinstance(raw, int) and raw > 0:
        return raw
    text = str(raw).strip()
    m = re.search(r"#(\d+)\b", text)
    if m:
        return int(m.group(1))
    m = _ISSUE_URL_RE.search(text)
    if m:
        return int(m.group(1))
    if text.isdigit():
        return int(text)
    return None


def _fleet_progress_issues(tree: dict[str, Any]) -> frozenset[int]:
    out: set[int] = set()
    for area in tree.get("areas") or []:
        for node in area.get("nodes") or []:
            status = node.get("status") or ""
            if status not in ("building", "partial"):
                continue
            links = node.get("links") or {}
            for raw in links.get("issues") or []:
                n = _parse_progress_issue_ref(raw)
                if n is not None:
                    out.add(n)
    return frozenset(out)


def _pin_status_raw(item: dict[str, Any]) -> str:
    raw = item.get("status")
    if raw is None:
        return "open"
    if raw in (
        "open",
        "issued",
        "blocked",
        "fixed",
        "merged",
        "harvested",
        "archived",
    ):
        return str(raw)
    return "open"


def _is_partial_note(note: str | None) -> bool:
    if not note:
        return False
    trimmed = note.strip()
    return bool(_PARTIAL_PREFIX.match(trimmed) and _PARTIAL_REF.search(trimmed))


def _pin_visual_partial(item: dict[str, Any]) -> bool:
    status = _pin_status_raw(item)
    if status not in ("open", "issued"):
        return False
    return _is_partial_note(item.get("agent_note"))


def _summarize_lifecycle(items: list[dict[str, Any]]) -> PinStatusSummaryOut:
    summary = PinStatusSummaryOut(
        total=0,
        untriaged=0,
        open=0,
        issued=0,
        blocked=0,
        fixed=0,
        merged=0,
        harvested=0,
    )
    counts = summary.model_dump()
    for item in items:
        status = _pin_status_raw(item)
        if status == "archived":
            continue
        counts["total"] += 1
        if item.get("status") is None:
            counts["untriaged"] += 1
        elif status in counts:
            counts[status] += 1
    return PinStatusSummaryOut.model_validate(counts)


def _summarize_operator(
    items: list[dict[str, Any]], fleet_issues: frozenset[int]
) -> PinOperatorBreakdownOut:
    counts = {
        "total": 0,
        "sent_to_queue": 0,
        "in_progress": 0,
        "delegated": 0,
        "fixed": 0,
        "merged": 0,
        "blocked": 0,
        "harvested": 0,
    }
    for item in items:
        status = _pin_status_raw(item)
        if status == "archived":
            continue
        counts["total"] += 1
        if item.get("status") is None:
            counts["sent_to_queue"] += 1
        if status == "open" or _pin_visual_partial(item):
            counts["in_progress"] += 1
        if status == "issued":
            counts["delegated"] += 1
        issue_num = _parse_issue_url(item.get("issue_url"))
        if issue_num is not None and issue_num in fleet_issues:
            counts["in_progress"] += 1
        if status in ("fixed", "merged", "blocked", "harvested"):
            counts[status] += 1
    return PinOperatorBreakdownOut.model_validate(counts)


def _load_fleet_issues() -> frozenset[int]:
    progress_file = progress_module.PROGRESS_FILE
    if not progress_file.is_file():
        return frozenset()
    try:
        tree, _, _ = progress_module._read_tree_snapshot(progress_file)
    except Exception:
        return frozenset()
    return _fleet_progress_issues(tree)


@router.get("/comments/summary", response_model=CommentSummaryOut)
def comments_summary(request: Request) -> CommentSummaryOut:
    items = _load(_dir(request) / _COMMENTS_FILE, "comments")
    fleet_issues = _load_fleet_issues()
    return CommentSummaryOut(
        operator=_summarize_operator(items, fleet_issues),
        lifecycle=_summarize_lifecycle(items),
    )
