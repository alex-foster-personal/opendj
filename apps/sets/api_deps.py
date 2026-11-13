"""Request-scoped session lookups shared by the recorded-sets routers.

:mod:`apps.sets.api` and :mod:`apps.sets.api_controls` both resolve a session
from a request through these helpers, so the share-host privacy rule lives in
one place.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from . import paths as sets_paths
from .sessions import Session, get_session


def _sets_root(request: Request) -> Path | None:
    root = getattr(request.app.state, "sets_root", None)
    if root is None or isinstance(root, Path):
        return root
    raise TypeError("app.state.sets_root must be pathlib.Path or None")


def _session_or_not_found(request: Request, session_id: str) -> Session:
    try:
        session = get_session(session_id, sets_root=_sets_root(request))
    except sets_paths.SessionPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _share_visible_session(request: Request, session_id: str) -> Session:
    """Keep private set metadata invisible to a configured share host."""
    session = _session_or_not_found(request, session_id)
    audience = getattr(request.state, "share_audience", "local")
    if audience == "share" and session.summary.share_state != "shared_cloud":
        raise HTTPException(status_code=404, detail="session not found")
    return session
