"""Session discovery helpers.

Plan 12-03 Step 2. Scans ``data/sets/`` for session directories with
a ``manifest.json`` and returns summary/full-view dataclasses. Used
by the CLI ``list``/``replay`` commands and by :mod:`apps.sets.api`.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths as sets_paths
from .manifest import Manifest, read_manifest


@dataclass
class SessionSummary:
    """Minimal session projection used by listing endpoints."""

    session_id: str
    started_at: str
    ended_at: str | None
    duration_s: float | None
    event_count: int
    transition_count: int
    share_state: str


@dataclass
class Session:
    """Full session projection (manifest + derived counts)."""

    summary: SessionSummary
    manifest: dict[str, Any]
    transitions: list[dict[str, Any]] = field(default_factory=list)
    labels: dict[int, str] = field(default_factory=dict)


def _duration_from_manifest(m: Manifest) -> float | None:
    if m.started_at is None or m.ended_at is None:
        return None
    try:
        from datetime import datetime

        started = datetime.fromisoformat(m.started_at)
        ended = datetime.fromisoformat(m.ended_at)
        return max(0.0, (ended - started).total_seconds())
    except ValueError:
        return None


def _transition_count(session_dir: Path) -> int:
    path = session_dir / "transitions.jsonl"
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def list_sessions(*, sets_root: Path | None = None) -> list[SessionSummary]:
    """Return :class:`SessionSummary` objects for every session dir.

    Sessions without a ``manifest.json`` are skipped.
    """
    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    if not root.exists():
        return []
    out: list[SessionSummary] = []
    for session_dir in sorted(root.iterdir()):
        if not session_dir.is_dir():
            continue
        if not (session_dir / "manifest.json").exists():
            continue
        manifest = read_manifest(session_dir)
        out.append(
            SessionSummary(
                session_id=manifest.session_id,
                started_at=manifest.started_at,
                ended_at=manifest.ended_at,
                duration_s=_duration_from_manifest(manifest),
                event_count=int(manifest.event_count or 0),
                transition_count=_transition_count(session_dir),
                share_state=manifest.share_state,
            )
        )
    return sorted(out, key=lambda s: s.started_at, reverse=True)


def get_session(session_id: str, *, sets_root: Path | None = None) -> Session | None:
    """Return the full :class:`Session` or ``None`` if missing."""
    root = Path(sets_root) if sets_root is not None else sets_paths.SETS_DIR
    session_dir = sets_paths.session_dir(session_id, root=root)
    if not (session_dir / "manifest.json").exists():
        return None
    manifest = read_manifest(session_dir)
    summary = SessionSummary(
        session_id=manifest.session_id,
        started_at=manifest.started_at,
        ended_at=manifest.ended_at,
        duration_s=_duration_from_manifest(manifest),
        event_count=int(manifest.event_count or 0),
        transition_count=_transition_count(session_dir),
        share_state=manifest.share_state,
    )
    transitions: list[dict[str, Any]] = []
    tpath = session_dir / "transitions.jsonl"
    if tpath.exists():
        for line in tpath.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                transitions.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    labels: dict[int, str] = {}
    lpath = session_dir / "labels.jsonl"
    if lpath.exists():
        for line in lpath.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            labels[int(row["idx"])] = str(row["class"])
    return Session(
        summary=summary,
        manifest=manifest.to_dict(),
        transitions=transitions,
        labels=labels,
    )


def summary_to_dict(s: SessionSummary) -> dict[str, Any]:
    return asdict(s)


__all__ = [
    "Session",
    "SessionSummary",
    "list_sessions",
    "get_session",
    "summary_to_dict",
]
