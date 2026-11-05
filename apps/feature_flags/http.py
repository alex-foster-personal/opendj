"""Delivery surface of ``apps.feature_flags``: the flags HTTP router and wire models.

Delivery layers (``apps.webui``, ``apps.engine_core``) import this module;
nothing else outside the package may.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from apps.feature_flags import (
    FlagState,
    store_build_refusal,
    store_profile_is_source,
)
from apps.feature_flags.profiles import selected_profile
from apps.shared.engine_app_state import require_app_state
from apps.shared.sandbox import is_sandboxed

flags_router = APIRouter(prefix="/flags", tags=["feature-flags"])

# Set by ``apps.engine_core.account.api`` after import so tests can monkeypatch
# ``account_api.is_sandboxed`` without retargeting (same global lookup as when
# ``read_flags`` lived in that module).
_sandboxed_probe: Callable[[], bool] | None = None


class RefusalOut(BaseModel):
    """The one refusal shape, so a control and its tooltip cannot disagree."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    ui_title: str


class FlagOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    flag_id: str
    enabled: bool
    default: bool
    overridden: bool
    owner: str
    note: str
    retire_by: str
    #: SAND-01. Non-null only when this flag is off BECAUSE the shipped App
    #: Store profile turned it off, and it carries the fourth refusal
    #: sentence. Null covers both "this flag is on" and "it is off for some
    #: other reason", which are the same fact from a control's point of view:
    #: nothing there entitles it to say "App Store".
    refusal: RefusalOut | None = None


class FlagsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: Absolute path of the flag file, present or not.
    path: str
    file_present: bool
    #: The named build profile this process resolved (``full`` when none was
    #: selected). A CONFIG fact.
    build_profile: str
    #: Whether this process is inside a macOS App Sandbox container. A
    #: RUNTIME fact, and reported beside the profile rather than folded into
    #: it because SAND-04 turns on the two being able to disagree: a
    #: mis-packaged bundle is sandboxed on the full profile, and a developer
    #: can run the store profile unsandboxed.
    sandboxed: bool
    flags: list[FlagOut]


def _store_build_refusal(flag: FlagState, *, from_store_profile: bool, sandboxed: bool) -> RefusalOut | None:
    """Adapt :func:`apps.feature_flags.store_build_refusal` to the account wire model.

    The decision itself lives in ``apps.feature_flags`` so the USB gate
    (``apps.webui.server.routes.usb_gate``) computes the SAME refusal from
    the SAME inputs this endpoint discloses, rather than each guessing at
    attribution on its own (SAND-01 review, PR #1668).
    """
    refusal = store_build_refusal(flag, from_store_profile=from_store_profile, sandboxed=sandboxed)
    if refusal is None:
        return None
    return RefusalOut(code=refusal.code, message=refusal.message, ui_title=refusal.ui_title)


def _process_is_sandboxed() -> bool:
    if _sandboxed_probe is not None:
        return _sandboxed_probe()
    return is_sandboxed()


@flags_router.get("", response_model=FlagsOut)
def read_flags(request: Request) -> FlagsOut:
    """The declared flags, as resolved once at engine boot.

    A separate surface from /entitlements on purpose (FLAG-01): these are
    engineering-owned code-path toggles, not anything an account is entitled
    to, and folding them into one response is the first step toward one store.
    """
    store = require_app_state(
        request,
        "feature_flags",
        missing=(
            "feature flags are not mounted on app.state.feature_flags; flags "
            "are read ONCE at startup and this route will not re-read the "
            "file per request to cover for a missing boot step"
        ),
    )
    from_store_profile = store_profile_is_source(store)
    sandboxed = _process_is_sandboxed()
    return FlagsOut(
        path=str(store.path),
        file_present=store.file_present,
        build_profile=selected_profile(),
        sandboxed=sandboxed,
        flags=[
            FlagOut(
                flag_id=flag.flag_id,
                enabled=flag.enabled,
                default=flag.default,
                overridden=flag.overridden,
                owner=flag.owner,
                note=flag.note,
                retire_by=flag.retire_by,
                refusal=_store_build_refusal(
                    flag,
                    from_store_profile=from_store_profile,
                    sandboxed=sandboxed,
                ),
            )
            for flag in store.snapshot()
        ],
    )
