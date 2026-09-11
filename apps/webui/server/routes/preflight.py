"""``GET /api/v1/preflight`` -- the boot gate (PREFLIGHT-01, issue #771).

Agent-native parity is the point of this issue: the UI, the CLI, the RC-QA
lane and the ship flow's OPS-05 probe all read THIS endpoint. There is no
UI-only pass/fail logic anywhere -- every verdict is computed in
:mod:`apps.webui.server.preflight_checks` and this route only serves it.

"Re-check" (poll) and "Re-request permissions" (the audio-access retry) are
the SAME GET: the audio-access check already performs the real gated read
every time it runs, so a second GET after granting the OS permission is
both the recheck and the re-request. There is no separate mutating route to
keep in sync with this one.

Requirements (mini-PRD):
  * ✔︎ 🎯 the response's ``status`` is ``fail`` iff any check is ``fail``; a
    ``pending`` check (e.g. audio-access with no resolvable sample) never
    blocks it.
    [if] a pending-only response reports overall ``fail`` [then ⛔️]
  * ✔︎ 🎯 every check row carries a real ``detail`` string; a check that
    could not be exercised is ``pending``, never a synthesized ``pass``.
    [if] a check with nothing to measure ever reports ``pass`` [then ⛔️]
  * ✔︎ 🎯 the audio-access timeout is enforced by this endpoint, not by the
    caller's HTTP client: a blocked ``open()`` (#766) must fail within
    ``AUDIO_ACCESS_TIMEOUT_S``, never hang the request.
    [if] a GET against a blocked audio path takes longer than the bound
    [then ⛔️]
"""
from __future__ import annotations

from fastapi import APIRouter

from apps.adapters.rekordbox import config as rb_config

from ..models import PreflightOut
from ..preflight_checks import run_preflight

router = APIRouter(prefix="/preflight", tags=["preflight"])


@router.get("", response_model=PreflightOut)
def read_preflight() -> PreflightOut:
    # rb_config.STATE_DB, not app.state.state_db_path: this is the same
    # module-level constant the rest of the rekordbox adapter reads, so the
    # check opens the SAME state.db a real boot would rather than a second,
    # divergent one.
    return run_preflight(rb_config.STATE_DB)


__all__ = ["router"]
