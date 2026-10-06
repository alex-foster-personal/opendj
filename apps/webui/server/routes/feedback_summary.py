"""GET /feedback/comments/summary -- operator pin buckets (FB-20, pin 6af63c5e9b7c).

Correlates comment pins with the progress-tree ledger when an issue_url matches
``links.issues`` on a node in ``building`` or ``partial`` status.

The ledger is a dev-checkout file (``data/progress-tree.yaml``); a packaged
desktop payload does not stage it. Its absence is reported as
``fleet_correlation: "ledger_missing"`` rather than read as "no fleet work", so
the UI can say the in-progress count excludes fleet correlation instead of
silently under-counting.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import progress as progress_module
from .feedback import _COMMENTS_FILE, _dir, _load

router = APIRouter(prefix="/feedback", tags=["feedback"])

_ISSUE_URL_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)(?:$|[/?#])"
)
_REPO_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")
# Bare ledger refs ("151", "#151") belong to this repository.
_HOME_REPO = "private_owner/music-dj-tools"
_KNOWN_STATUSES = frozenset(
    {"open", "issued", "blocked", "fixed", "merged", "harvested", "archived"}
)

#: An issue identity: (lowercased "owner/repo", number). The number alone is
#: not an identity -- another repository can have the same issue number.
IssueKey = tuple[str, int]
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


FleetCorrelation = Literal["ok", "ledger_missing", "ledger_unreadable"]


class CommentSummaryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    operator: PinOperatorBreakdownOut
    lifecycle: PinStatusSummaryOut
    # Whether operator.in_progress includes progress-tree correlation. Anything
    # but "ok" means fleet work in flight was NOT measured, not that there is none.
    fleet_correlation: FleetCorrelation


def _parse_issue_url(issue_url: str | None) -> IssueKey | None:
    if not issue_url:
        return None
    m = _ISSUE_URL_RE.search(issue_url)
    if not m:
        return None
    return (f"{m.group(1)}/{m.group(2)}".lower(), int(m.group(3)))


def _parse_progress_issue_ref(raw: str | int) -> IssueKey | None:
    if isinstance(raw, int) and raw > 0:
        return (_HOME_REPO, raw)
    text = str(raw).strip()
    m = _ISSUE_URL_RE.search(text)
    if m:
        return (f"{m.group(1)}/{m.group(2)}".lower(), int(m.group(3)))
    m = _REPO_REF_RE.search(text)
    if m:
        return (m.group(1).lower(), int(m.group(2)))
    m = re.search(r"#(\d+)\b", text)
    if m:
        return (_HOME_REPO, int(m.group(1)))
    if text.isdigit():
        return (_HOME_REPO, int(text))
    return None


def _fleet_progress_issues(tree: dict[str, Any]) -> frozenset[IssueKey]:
    out: set[IssueKey] = set()
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
        # Legacy untriaged pin: the lifecycle view shows it as open, and the
        # operator view counts it as sent to queue (never as in progress).
        return "open"
    if raw in _KNOWN_STATUSES:
        return str(raw)
    # An unknown persisted status is malformed data or lifecycle drift. Read as
    # "open" it would be counted in a bucket it never claimed, so refuse loudly.
    raise HTTPException(
        status_code=500,
        detail={
            "error": "unknown_pin_status",
            "pin_id": item.get("id"),
            "status": raw,
            "message": f"pin status {raw!r} is not one of {sorted(_KNOWN_STATUSES)}",
        },
    )


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
    items: list[dict[str, Any]], fleet_issues: frozenset[IssueKey]
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
        if status == "issued":
            counts["delegated"] += 1
        issue_key = _parse_issue_url(item.get("issue_url"))
        fleet_match = issue_key is not None and issue_key in fleet_issues
        # A raw-null pin is untriaged (sent to queue) and must not read as
        # active work through ANY signal -- explicit open, a PARTIAL note, or
        # fleet correlation -- even though _pin_status_raw normalizes it to
        # "open" for the lifecycle view.
        triaged = item.get("status") is not None
        explicit_open = item.get("status") == "open"
        if triaged and (explicit_open or _pin_visual_partial(item) or fleet_match):
            counts["in_progress"] += 1
        if status in ("fixed", "merged", "blocked", "harvested"):
            counts[status] += 1
    return PinOperatorBreakdownOut.model_validate(counts)


def _load_fleet_issues() -> tuple[frozenset[IssueKey], FleetCorrelation]:
    progress_file = progress_module.PROGRESS_FILE
    if not progress_file.is_file():
        return frozenset(), "ledger_missing"
    try:
        tree, _, _ = progress_module._read_tree_snapshot(progress_file)
        return _fleet_progress_issues(tree), "ok"
    except (
        HTTPException,
        yaml.YAMLError,
        OSError,
        UnicodeDecodeError,
        KeyError,
        TypeError,
        AttributeError,
    ):
        # A broken ledger must not take the pin hover down with it, but it is
        # reported as unmeasured, never as zero fleet work.
        return frozenset(), "ledger_unreadable"


@router.get("/comments/summary", response_model=CommentSummaryOut)
def comments_summary(request: Request) -> CommentSummaryOut:
    items = _load(_dir(request) / _COMMENTS_FILE, "comments")
    fleet_issues, fleet_correlation = _load_fleet_issues()
    return CommentSummaryOut(
        operator=_summarize_operator(items, fleet_issues),
        lifecycle=_summarize_lifecycle(items),
        fleet_correlation=fleet_correlation,
    )
