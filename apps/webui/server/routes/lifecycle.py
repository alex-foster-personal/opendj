"""``POST /api/v1/lifecycle/quit`` -- force-quit the desktop shell (INSTALL-21, OPS-07).

The ship flow and virgin-boot clean quit call this loopback-only route so the
shell exits without the in-app confirmation dialog. The frontend subscribes to
the published ``shell.quit`` bus event and calls the same confirm path as a
user-confirmed quit, including the session snapshot flush.

The 200 below means "the shell.quit event was published", not "the shell
exited": ``publish()`` (apps/shared/events.py) is fire-and-forget with no
reply channel, so this route cannot know whether the frontend's exit() call
later succeeds or is refused by the Tauri ACL (issue #3058). The frontend
reports a failed exit itself via the client-error pipeline (quit-gate.ts's
confirmQuit), and the ship-flow caller (ship_dmg.sh's _m_quit_one_app) treats
a still-running app after its deadline as this route having failed, since
that is the only reliable place the outcome is observable.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from apps.shared.events import publish

from .cloudsync_ops import LOCAL_ONLY_RESPONSE, _refuse, require_local_operator

router = APIRouter(prefix="/lifecycle", tags=["lifecycle"])


class LifecycleQuitIn(BaseModel):
    force: bool = Field(description="Must be true for agent and ship clean-quit paths.")


class LifecycleQuitOut(BaseModel):
    ok: bool = True


@router.post(
    "/quit",
    response_model=LifecycleQuitOut,
    responses=LOCAL_ONLY_RESPONSE,
    dependencies=[Depends(require_local_operator)],
)
def request_shell_quit(body: LifecycleQuitIn) -> LifecycleQuitOut:
    if not body.force:
        raise _refuse(
            422,
            "LIFECYCLE_QUIT_FORCE_REQUIRED",
            'POST /api/v1/lifecycle/quit requires {"force": true} for the clean-quit path.',
        )
    publish("shell.quit", {"force": True, "source": "lifecycle"})
    return LifecycleQuitOut()


__all__ = ["router"]
