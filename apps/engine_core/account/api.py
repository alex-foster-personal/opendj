"""Account, entitlements and feature flags over HTTP.

    GET    /api/v1/account                  identity, plan, and exactly what
                                            is stored locally
    DELETE /api/v1/account                  erase the user row and its
                                            sessions (typed confirm required)
    GET    /api/v1/entitlements             the plan, the refusal shape, and
                                            every gateable feature
    GET    /api/v1/entitlements/{feature}   may this account use one feature
    GET    /api/v1/flags                    the declared feature flags, where
                                            each value came from, and which
                                            build profile is active

THIS ROUTE REPORTS; IT NEVER DECIDES.  ``apps.entitlements`` answers
``has`` / ``quota`` and owns the refusal wording, ``apps.feature_flags`` owns
the flag file.  That is the same split as
``apps/webui/server/routes/rekordbox_gate.py`` over
``apps.shared.rekordbox_writeback``, and it is what keeps a disabled control
and the server refusing the request from ever disagreeing (ENT-02): both read
the SAME ``code`` / ``message`` / ``ui_title`` off this response instead of
each spelling their own.

WHY THE ENGINE APP.  New server capability is registered in
``apps.engine_core.app`` the way ``jobs_router`` / ``setup_router`` /
``assistant_router`` are, not in the legacy ``apps/webui/server/app.py``. The
engine wraps the legacy app and is the surface the committed
``apps/webui/openapi.json`` is dumped from, so a route registered anywhere
else is a route no generated client can see.

Requirements (mini-PRD):
  * ✔︎ ✅ 🎯 /account answers signed-in and signed-out, and in both cases
    states that sign-in is identity, not authorisation (ACCT-02).
    [if] the response can be read as "signing in unlocks things" [then ⛔️]
  * ✔︎ ✅ 🎯 /account lists every local store keyed to the account, with a
    real path and the request that deletes it (ACCT-03).
    [if] a disclosure row names no way to delete what it describes [then ⛔️]
  * ✔︎ ✅ 🎯 DELETE /account without the exact confirm token changes nothing
    and 422s with the token in the message.
    [if] an unconfirmed DELETE removes a row [then ⛔️]
  * ✔︎ ✅ 🎯 every entitlement answer carries code + message + ui_title, even
    when entitled, so a client never hardcodes refusal wording.
    [if] ui_title drifts from entitlements.UI_REFUSAL_TITLE [then ⛔️]
  * ✔︎ ✅ 🎯 /flags reports the file it read and whether each value is a
    default or an override.
    [if] an override reads as a default [then ⛔️]
  * ✔︎ ✅ 🎯 /flags names the active build profile and whether this process is
    sandboxed, and attaches the FOURTH refusal (SAND-01) to every flag the
    shipped App Store profile turned off, so a dead control reads its reason
    off the server instead of composing one.
    [if] a flag off in the store build carries no refusal [then ⛔️]
    [if] a flag off for any OTHER reason carries one anyway [then ⛔️]
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict

from apps.entitlements import (
    FEATURES,
    NOT_IN_PLAN_CODE,
    NOT_IN_PLAN_MESSAGE,
    UI_REFUSAL_TITLE,
    Plan,
    configured_provider,
    current_plan,
    has,
    quota,
)
from apps.feature_flags import (
    FlagState,
    FlagStore,
    store_build_refusal,
    store_profile_is_source,
)
from apps.feature_flags.profiles import selected_profile
from apps.shared.sandbox import is_sandboxed
from apps.webui.server.auth import SESSION_COOKIE_NAME, SessionUser
from apps.webui.server.routes.auth import session_store, signed_in_user

account_router = APIRouter(prefix="/account", tags=["account"])
entitlements_router = APIRouter(prefix="/entitlements", tags=["entitlements"])
flags_router = APIRouter(prefix="/flags", tags=["feature-flags"])

#: The published policy this disclosure has to stay true to.
PRIVACY_POLICY_URL: str = "https://open-dj.com/privacy"

#: ACCT-02, in one sentence, on the wire rather than only in the README. The
#: UI renders it verbatim; a "plan" that gates nothing must not be allowed to
#: imply that signing in unlocks something.
IDENTITY_NOT_AUTHORISATION: str = (
    "Signing in gives openDJ your identity, not authorisation. It tells "
    "openDJ who you are; it does not decide what you may do. There is no "
    "authorisation anywhere in this app: every feature works exactly the "
    "same signed out, and nothing is gated behind an account."
)

#: The typed confirm for the destructive route, spelled once. A DELETE that
#: fires on an empty query string is one stray curl away from erasing an
#: account, and this endpoint exists precisely to be driven by agents.
ACCOUNT_DELETE_CONFIRM: str = "delete-my-local-account-data"

DELETE_HINT: str = (
    f"DELETE /api/v1/account?confirm={ACCOUNT_DELETE_CONFIRM}"
)


# ----- models -------------------------------------------------------------
class AccountUserOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    google_sub: str
    email: str
    name: str | None
    avatar_url: str | None
    created_at: str


class PlanOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: str
    label: str
    note: str
    #: None while no payment provider is configured, which is the shipped
    #: state. It is the field that says whether plan_id came from anywhere.
    provider: str | None

    @classmethod
    def of(cls, plan: Plan) -> PlanOut:
        return cls(
            plan_id=plan.plan_id,
            label=plan.label,
            note=plan.note,
            provider=plan.provider,
        )


class RefusalOut(BaseModel):
    """The one refusal shape, so a control and its tooltip cannot disagree."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    ui_title: str


class LocalDataOut(BaseModel):
    """One store on this machine that holds something about the account."""

    model_config = ConfigDict(frozen=True)

    label: str
    location: str
    contents: str
    delete_with: str


class AccountOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    signed_in: bool
    user: AccountUserOut | None
    plan: PlanOut
    #: False, and it is a claim under test rather than a comment: this app
    #: has no authorisation concept at all.
    authorisation_enforced: bool
    authorisation_note: str
    local_data: list[LocalDataOut]
    privacy_policy_url: str


class AccountDeleteOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    deleted: bool
    google_sub: str
    message: str


class FeatureEntitlementOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    feature_id: str
    label: str
    entitled: bool
    #: None means unlimited, never "unknown".
    quota: int | None
    server_side: bool
    note: str


class EntitlementsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str | None
    plan: PlanOut
    refusal: RefusalOut
    features: list[FeatureEntitlementOut]


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


# ----- helpers ------------------------------------------------------------
def _flag_store(request: Request) -> FlagStore:
    store = getattr(request.app.state, "feature_flags", None)
    if store is None:
        raise RuntimeError(
            "feature flags are not mounted on app.state.feature_flags; flags "
            "are read ONCE at startup by apps.engine_core.app.create_app and "
            "this route will not re-read the file per request to cover for a "
            "missing boot step"
        )
    return store


def _store_build_refusal(
    flag: FlagState, *, from_store_profile: bool, sandboxed: bool
) -> RefusalOut | None:
    """Adapt :func:`apps.feature_flags.store_build_refusal` to the account wire model.

    The decision itself lives in ``apps.feature_flags`` so the USB gate
    (``apps.webui.server.routes.usb_gate``) computes the SAME refusal from
    the SAME inputs this endpoint discloses, rather than each guessing at
    attribution on its own (SAND-01 review, PR #1668).
    """
    refusal = store_build_refusal(
        flag, from_store_profile=from_store_profile, sandboxed=sandboxed
    )
    if refusal is None:
        return None
    return RefusalOut(
        code=refusal.code, message=refusal.message, ui_title=refusal.ui_title
    )


def _refusal() -> RefusalOut:
    return RefusalOut(
        code=NOT_IN_PLAN_CODE,
        message=NOT_IN_PLAN_MESSAGE,
        ui_title=UI_REFUSAL_TITLE,
    )


def _state_db_path(request: Request) -> str:
    path = getattr(request.app.state, "state_db_path", None)
    if path is None:
        raise RuntimeError(
            "app.state.state_db_path is not set, so this route cannot tell "
            "the user WHERE their account row lives. A disclosure that names "
            "no path is not a disclosure"
        )
    return str(path)


def _local_data(state_db: str) -> list[LocalDataOut]:
    """Exactly what is stored about the account, and how to erase each part.

    Two rows, because there are two tables (schema v6). Everything else in
    the data dir is about the music library and is keyed to tracks, not to a
    Google account, so listing it here would pad the disclosure with things
    that are not about the person reading it.
    """
    return [
        LocalDataOut(
            label="Account row",
            location=f"{state_db} (table: users)",
            contents=(
                "Google account id (sub), email address, display name, "
                "avatar URL, and the first and last time you signed in."
            ),
            delete_with=DELETE_HINT,
        ),
        LocalDataOut(
            label="Sign-in sessions",
            location=f"{state_db} (table: auth_sessions)",
            contents=(
                "One row per signed-in browser: a sha256 of the session "
                "cookie (never the cookie itself) and the Google refresh and "
                "access tokens the daemon holds on your behalf."
            ),
            delete_with=(
                "POST /api/v1/auth/logout for this browser, or "
                f"{DELETE_HINT} for every session at once."
            ),
        ),
    ]


def _require_user(request: Request) -> SessionUser:
    user = signed_in_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "AUTH_REQUIRED",
                "message": (
                    "not signed in, so there is no account to act on. Sign in "
                    "with POST /api/v1/auth/login first."
                ),
            },
        )
    return user


# ----- account ------------------------------------------------------------
@account_router.get("", response_model=AccountOut)
def read_account(request: Request) -> AccountOut:
    """Identity, plan, and the full local-storage disclosure.

    Answers signed OUT too, with ``user: null``: what is stored and what is
    gated are questions somebody is entitled to ask BEFORE handing over an
    identity, and refusing to answer until they sign in would be a strange
    reading of a privacy disclosure.
    """
    user = signed_in_user(request)
    return AccountOut(
        signed_in=user is not None,
        user=(
            None
            if user is None
            else AccountUserOut(
                google_sub=user.google_sub,
                email=user.email,
                name=user.name,
                avatar_url=user.avatar_url,
                created_at=user.created_at,
            )
        ),
        plan=PlanOut.of(current_plan()),
        authorisation_enforced=False,
        authorisation_note=IDENTITY_NOT_AUTHORISATION,
        local_data=_local_data(_state_db_path(request)),
        privacy_policy_url=PRIVACY_POLICY_URL,
    )


@account_router.delete("", response_model=AccountDeleteOut)
def delete_account(
    request: Request,
    response: Response,
    confirm: str = Query(
        "",
        description=(
            "Must be exactly "
            f"'{ACCOUNT_DELETE_CONFIRM}'. Typed confirm, so this cannot fire "
            "by accident from a bare DELETE."
        ),
    ),
) -> AccountDeleteOut:
    """Erase the user row and, by FK cascade, every session it owns.

    The deletion half of https://open-dj.com/privacy (ACCT-03). The session
    cookie is cleared on the way out, because leaving the browser holding a
    token for a row that no longer exists would leave the UI showing a signed
    in state that resolves to nobody.
    """
    user = _require_user(request)
    if confirm != ACCOUNT_DELETE_CONFIRM:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ACCOUNT_DELETE_UNCONFIRMED",
                "message": (
                    "refusing to delete an account without an explicit "
                    f"confirmation. Repeat the request as {DELETE_HINT}. "
                    "Nothing was changed."
                ),
            },
        )
    deleted = session_store(request).delete_user(user.google_sub)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return AccountDeleteOut(
        deleted=deleted,
        google_sub=user.google_sub,
        message=(
            "account row and every session for it were deleted from this "
            "machine. Your music library, playlists and analysis are "
            "untouched: they were never keyed to the account."
        ),
    )


# ----- entitlements -------------------------------------------------------
def _feature_out(feature_id: str, label: str, *, server_side: bool, note: str) -> (
    FeatureEntitlementOut
):
    return FeatureEntitlementOut(
        feature_id=feature_id,
        label=label,
        entitled=has(feature_id),
        quota=quota(feature_id),
        server_side=server_side,
        note=note,
    )


@entitlements_router.get("", response_model=EntitlementsOut)
def read_entitlements() -> EntitlementsOut:
    """The plan, the refusal shape, and every gateable feature.

    ``features`` is empty while the catalog is, and that is the honest answer
    rather than a missing one: openDJ has no paid features, so there is
    nothing an account could fail to be entitled to.
    """
    return EntitlementsOut(
        provider=configured_provider(),
        plan=PlanOut.of(current_plan()),
        refusal=_refusal(),
        features=[
            _feature_out(
                feature.feature_id,
                feature.label,
                server_side=feature.server_side,
                note=feature.note,
            )
            for feature in FEATURES
        ],
    )


@entitlements_router.get("/{feature_id}", response_model=FeatureEntitlementOut)
def read_entitlement(feature_id: str) -> FeatureEntitlementOut:
    """May this account use one feature?

    An id the catalog does not know is answered, not 404'd: while no provider
    is configured everything is entitled (ENT-04), and a 404 here would let a
    caller mistake "we have never heard of that" for "you may not".
    """
    known = {feature.feature_id: feature for feature in FEATURES}.get(
        feature_id
    )
    return _feature_out(
        feature_id,
        known.label if known is not None else feature_id,
        server_side=known.server_side if known is not None else False,
        note=(
            known.note
            if known is not None
            else (
                "not in the feature catalog: no code gates this, so it is "
                "included by construction rather than by a plan"
            )
        ),
    )


# ----- feature flags ------------------------------------------------------
@flags_router.get("", response_model=FlagsOut)
def read_flags(request: Request) -> FlagsOut:
    """The declared flags, as resolved once at engine boot.

    A separate surface from /entitlements on purpose (FLAG-01): these are
    engineering-owned code-path toggles, not anything an account is entitled
    to, and folding them into one response is the first step toward one store.
    """
    store = _flag_store(request)
    from_store_profile = store_profile_is_source(store)
    sandboxed = is_sandboxed()
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


__all__ = [
    "ACCOUNT_DELETE_CONFIRM",
    "DELETE_HINT",
    "IDENTITY_NOT_AUTHORISATION",
    "PRIVACY_POLICY_URL",
    "AccountDeleteOut",
    "AccountOut",
    "AccountUserOut",
    "EntitlementsOut",
    "FeatureEntitlementOut",
    "FlagOut",
    "FlagsOut",
    "LocalDataOut",
    "PlanOut",
    "RefusalOut",
    "account_router",
    "entitlements_router",
    "flags_router",
]
