"""Session discovery helpers.

Plan 12-03 Step 2. Scans ``(private path)/`` for session directories with
a ``manifest.json`` and returns summary/full-view dataclasses. Used
by the CLI ``list``/``replay`` commands and by :mod:`apps.sets.api`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths as sets_paths
from .classify import read_transitions
from .label import read_labels
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
        started = datetime.fromisoformat(m.started_at)
        ended = datetime.fromisoformat(m.ended_at)
        return max(0.0, (ended - started).total_seconds())
    except ValueError:
        return None


def _session_summary(manifest: Manifest, session_dir: Path) -> SessionSummary:
    return SessionSummary(
        session_id=manifest.session_id,
        started_at=manifest.started_at,
        ended_at=manifest.ended_at,
        duration_s=_duration_from_manifest(manifest),
        event_count=int(manifest.event_count or 0),
        transition_count=_transition_count(session_dir),
        share_state=manifest.share_state,
    )


def _transition_count(session_dir: Path) -> int:
    path = session_dir / "transitions.jsonl"
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def list_sessions(*, sets_root: Path | None = None) -> list[SessionSummary]:
    """Return :class:`SessionSummary` objects for every session dir.

    Sessions without a ``manifest.json`` are skipped.
    """
    root = sets_paths.resolve_sets_root(sets_root)
    if not root.exists():
        return []
    out: list[SessionSummary] = []
    for session_dir in sorted(root.iterdir()):
        if not session_dir.is_dir():
            continue
        if not (session_dir / "manifest.json").exists():
            continue
        manifest = read_manifest(session_dir)
        out.append(_session_summary(manifest, session_dir))
    return sorted(out, key=lambda s: s.started_at, reverse=True)


def get_session(session_id: str, *, sets_root: Path | None = None) -> Session | None:
    """Return the full :class:`Session` or ``None`` if missing.

    Raises ``ValueError`` when ``manifest.json`` names a different session_id
    than the requested directory.
    """
    root = sets_paths.resolve_sets_root(sets_root)
    session_dir = sets_paths.session_dir(session_id, root=root)
    if not (session_dir / "manifest.json").exists():
        return None
    manifest = read_manifest(session_dir)
    if manifest.session_id != session_id:
        raise ValueError(
            f"manifest session_id {manifest.session_id!r} does not match "
            f"requested {session_id!r}"
        )
    summary = _session_summary(manifest, session_dir)
    transitions = read_transitions(session_id, sets_root=root)
    labels = read_labels(session_id, sets_root=root)
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
