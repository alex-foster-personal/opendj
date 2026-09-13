"""Worktree port reservation routes.

Requirements (mini-PRD):
  GET /admin/ports: the reserved backend/frontend pair for this worktree,
  matching ``python -m apps.webui.port_config show --json``.
Acceptance:
  [if] this worktree does not own the pair [then] 503 with code
       worktree_ports_unreserved
  [if] the pair is reserved [then] 200 with backend, frontend, api_proxy_target
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from apps.webui.port_config import PortConfigError, show_ports

router = APIRouter(prefix="/admin", tags=["admin"])


class WorktreePortsOut(BaseModel):
    backend: int
    frontend: int
    api_proxy_target: str


@router.get("/ports")
def get_worktree_ports() -> WorktreePortsOut:
    try:
        ports = show_ports()
    except PortConfigError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "worktree_ports_unreserved", "message": str(exc)},
        ) from exc
    return WorktreePortsOut(
        backend=ports.backend,
        frontend=ports.frontend,
        api_proxy_target=ports.api_proxy_target,
    )
