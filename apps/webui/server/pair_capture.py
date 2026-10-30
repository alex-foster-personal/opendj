"""The hand-mix capture hook on the mirror route (MEMORY-04, ADR-0129).

``routes/state.py`` hands every accepted mirror to :func:`observe_mirror`. A closed session is put on both tracks' grids,
simplified, validated and saved on one worker thread, in close order, so the once-a-second PUT never waits on analysis
and an older mix can never land after a newer one. Capture
never fails a publish: the mirror is what every agent and the order poll stand on, so a capture fault is logged and
shown by ``GET /api/v1/autodj/pair-transition/capture``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request

from apps.autodj.capture import ClosedMix, TransitionCapture, in_bars
from apps.autodj.pair_memory import record_played
from apps.autodj.plan import validate_plan
from apps.autodj.profile import TrackProfile
from apps.autodj.simplify import SimplifyError, simplify
from apps.shared.engine_origin import EngineOrigin
from apps.shared.state import db as state_db
from apps.transition_score.tryer import EngineProfiles

from .deps import get_library_state_db

_LOG = logging.getLogger(__name__)

ProfileSource = Callable[[str], TrackProfile]


def capture_of(app: FastAPI) -> TransitionCapture:
    capture = getattr(app.state, "pair_capture", None)
    if capture is None:
        capture = app.state.pair_capture = TransitionCapture()
    return capture


def _saver_of(app: FastAPI) -> ThreadPoolExecutor:
    """One worker, so saves land in the order their sessions closed."""
    saver = getattr(app.state, "pair_capture_saver", None)
    if saver is None:
        saver = app.state.pair_capture_saver = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pair-capture")
    return saver


def _engine_profiles(request: Request, path: Path) -> ProfileSource:
    server = request.scope.get("server")
    if not server or server[1] is None:
        raise RuntimeError(f"the mirror route cannot name its own socket (scope server={server!r})")
    host, port = server
    origin = EngineOrigin(host=host, port=int(port), lock_path=path, pid=None, role=None, boot_id=None)
    return EngineProfiles(origin).profile


def finish_capture(mix: ClosedMix, capture: TransitionCapture, path: Path, profile_of: ProfileSource) -> None:
    """Put one closed mix on the plan clock, simplify, validate and save it; its verdict lands on ``capture``'s status."""
    out_profile, in_profile = profile_of(mix.out_track), profile_of(mix.in_track)
    try:
        plan = simplify(in_bars(mix, out_profile, in_profile))
    except SimplifyError as error:
        capture.note_result("not_saved", str(error))
        return
    validation = validate_plan(plan, out_profile, in_profile)
    if not validation.ok:
        capture.note_result("refused", "; ".join(validation.errors))
        _LOG.warning("captured mix %s into %s refused: %s", plan.out.track, plan.in_.track, validation.errors)
        return
    conn = state_db.open_rw(path)
    try:
        with state_db.write_unit(conn, "pair_capture"):
            record_played(
                conn, plan.out.track, plan.in_.track, plan=plan.to_json(), source="captured", style=plan.style
            )
    finally:
        conn.close()
    capture.note_result("saved", plan.style)


def _finish_guarded(mix: ClosedMix, capture: TransitionCapture, path: Path, profile_of: ProfileSource) -> None:
    try:
        finish_capture(mix, capture, path, profile_of)
    except Exception as error:  # noqa: BLE001  # a worker thread's last stop: logged and shown, never lost
        _LOG.exception("saving the captured mix %s into %s failed", mix.out_track, mix.in_track)
        capture.note_result("failed", str(error))


def observe_mirror(request: Request, mirror: Mapping[str, Any]) -> None:
    capture = capture_of(request.app)
    try:
        mix = capture.observe(mirror)
    except (ValueError, TypeError) as error:
        _LOG.exception("hand-mix capture could not read a published mirror")
        capture.note_fault(str(error))
        return
    if mix is None:
        return
    path = get_library_state_db(request)
    source: ProfileSource | None = getattr(request.app.state, "pair_capture_profile_source", None)
    profile_of = source if source is not None else _engine_profiles(request, path)
    _saver_of(request.app).submit(_finish_guarded, mix, capture, path, profile_of)
