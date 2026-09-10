"""The account / entitlements / flags HTTP surface, driven the way an agent would.

ACCT-04 claims agent parity for every account action, and ENT-02 claims a
disabled control and the server refusing the request can never disagree.
Neither is true until something walks the endpoints over HTTP with no browser
anywhere, which is what this module does -- including the destructive one,
against a real sqlite state db.

Regression lines:
  - if an account endpoint disappears from the committed contract then an
    agent driving the panel breaks with no test failing
  - if /account stops stating that sign-in is identity rather than
    authorisation then the UI is free to imply the opposite (ACCT-02)
  - if a disclosure row names no path or no way to delete it then ACCT-03 is
    prose rather than a promise
  - if an unconfirmed DELETE removes a row then a stray curl erases an account
  - if a delete leaves the session rows behind then the Google tokens outlive
    the account they belonged to
  - if an entitlement response drops code/message/ui_title then a client has
    to hardcode refusal wording
  - if /flags reports an override as a default then the flag file is lying
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.account.api import (
    ACCOUNT_DELETE_CONFIRM,
    IDENTITY_NOT_AUTHORISATION,
    account_router,
    entitlements_router,
    flags_router,
)
from apps.entitlements import (
    NOT_IN_PLAN_CODE,
    NOT_IN_PLAN_MESSAGE,
    PROVIDER_ENV,
    UI_REFUSAL_TITLE,
)
from apps.feature_flags import FLAGS_FILENAME, FlagDef, load_flags
from apps.feature_flags.profiles import (
    BUILD_PROFILE_ENV,
    DEFAULT_PROFILE,
    STORE_PROFILE,
)
from apps.shared.sandbox import (
    STORE_BUILD_REFUSAL_CODE,
    STORE_BUILD_REFUSAL_TITLE,
)
from apps.shared.state import db as state_db
from apps.webui.server.auth import (
    SESSION_COOKIE_NAME,
    GoogleIdentity,
    SessionStore,
)

ACCOUNT_PATHS: tuple[str, ...] = (
    "/api/v1/account",
    "/api/v1/entitlements",
    "/api/v1/entitlements/{feature_id}",
    "/api/v1/flags",
)

COMMITTED_OPENAPI: Path = (
    Path(__file__).resolve().parents[2] / "apps" / "webui" / "openapi.json"
)

TEST_DEFS: tuple[FlagDef, ...] = (
    FlagDef(
        flag_id="example.off_by_default",
        default=False,
        owner="tests",
        note="declared only inside this module",
        retire_by="never - test fixture",
        sandbox_gated=False,
    ),
)

IDENTITY = GoogleIdentity(
    google_sub="115551234567890",
    email="dj@example.com",
    name="Test DJ",
    avatar_url="https://lh3.googleusercontent.com/a/example",
    refresh_token="refresh-token-value",
    access_token="access-token-value",
    access_expires_at="2026-09-01T00:00:00+00:00",
)


@pytest.fixture(autouse=True)
def _inert_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped state: no payment provider, asserted not inherited."""
    monkeypatch.delenv(PROVIDER_ENV, raising=False)
    monkeypatch.delenv("MDT_FEATURE_FLAGS_FILE", raising=False)
    # A build profile leaking in from the ambient environment would silently
    # change which file every flag test reads.
    monkeypatch.delenv(BUILD_PROFILE_ENV, raising=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    (target / "state").mkdir(parents=True)
    return target


@pytest.fixture
def state_db_path(data_dir: Path) -> Path:
    path = data_dir / "state" / "state.db"
    # A REAL migrated database, not a stub: the delete under test relies on
    # the auth_sessions -> users FK cascade that only exists in the schema.
    state_db.open_rw(path).close()
    return path


@pytest.fixture
def client(data_dir: Path, state_db_path: Path) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(entitlements_router, prefix="/api/v1")
    app.include_router(flags_router, prefix="/api/v1")
    app.include_router(account_router, prefix="/api/v1")
    app.state.state_db_path = str(state_db_path)
    app.state.feature_flags = load_flags(data_dir, defs=TEST_DEFS)
    with TestClient(app) as test_client:
        yield test_client


def _sign_in(client: TestClient, state_db_path: Path) -> str:
    """Plant a real session, the way the OAuth callback does."""
    token = SessionStore(state_db_path).sign_in(IDENTITY)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return token


def _rows(state_db_path: Path, table: str) -> int:
    conn = state_db.open_rw(state_db_path)
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


# ----- the contract -------------------------------------------------------
def test_every_account_path_is_in_the_committed_openapi() -> None:
    schema = json.loads(COMMITTED_OPENAPI.read_text(encoding="utf-8"))
    missing = [path for path in ACCOUNT_PATHS if path not in schema["paths"]]
    assert missing == [], (
        f"{missing} are served but absent from the committed contract; run "
        "`just openapi-dump` and `pnpm run api:gen`"
    )


# ----- ACCT-01 / ACCT-02 --------------------------------------------------
def test_account_answers_signed_out_with_the_full_disclosure(
    client: TestClient,
) -> None:
    body = client.get("/api/v1/account").json()
    assert body["signed_in"] is False
    assert body["user"] is None
    # Asking what is stored and what is gated must not require handing over an
    # identity first.
    assert len(body["local_data"]) >= 2
    assert body["privacy_policy_url"] == "https://open-dj.com/privacy"


def test_account_states_that_sign_in_is_identity_not_authorisation(
    client: TestClient,
) -> None:
    body = client.get("/api/v1/account").json()
    assert body["authorisation_enforced"] is False
    assert body["authorisation_note"] == IDENTITY_NOT_AUTHORISATION
    lowered = body["authorisation_note"].lower()
    assert "identity" in lowered
    assert "authorisation" in lowered
    # ACCT-02 again, from the other side: the plan must not read as a free
    # tier of something purchasable.
    assert body["plan"]["provider"] is None
    assert "no paid features" in body["plan"]["note"]


def test_account_reports_the_signed_in_identity(
    client: TestClient, state_db_path: Path
) -> None:
    _sign_in(client, state_db_path)
    body = client.get("/api/v1/account").json()
    assert body["signed_in"] is True
    assert body["user"]["email"] == IDENTITY.email
    assert body["user"]["google_sub"] == IDENTITY.google_sub
    assert body["user"]["name"] == IDENTITY.name


# ----- ACCT-03: the disclosure ---------------------------------------------
def test_every_disclosure_row_names_a_real_path_and_a_way_to_delete_it(
    client: TestClient, state_db_path: Path
) -> None:
    body = client.get("/api/v1/account").json()
    for row in body["local_data"]:
        assert str(state_db_path) in row["location"], row
        assert row["contents"].strip() != "", row
        assert row["delete_with"].strip() != "", row
        # A disclosure that says "contact support" is not a path; each row has
        # to name the request that actually erases it.
        assert "/api/v1/" in row["delete_with"], row


def test_the_disclosure_covers_both_tables_that_hold_the_user(
    client: TestClient,
) -> None:
    locations = " ".join(
        row["location"] for row in client.get("/api/v1/account").json()["local_data"]
    )
    assert "users" in locations
    assert "auth_sessions" in locations


# ----- the destructive route ----------------------------------------------
def test_delete_without_the_confirm_token_changes_nothing(
    client: TestClient, state_db_path: Path
) -> None:
    _sign_in(client, state_db_path)
    response = client.delete("/api/v1/account")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "ACCOUNT_DELETE_UNCONFIRMED"
    # The refusal has to carry the token, or the caller cannot act on it.
    assert ACCOUNT_DELETE_CONFIRM in detail["message"]
    assert _rows(state_db_path, "users") == 1


def test_delete_with_a_wrong_confirm_token_changes_nothing(
    client: TestClient, state_db_path: Path
) -> None:
    _sign_in(client, state_db_path)
    response = client.delete("/api/v1/account?confirm=yes")
    assert response.status_code == 422
    assert _rows(state_db_path, "users") == 1


def test_delete_when_signed_out_is_a_401_not_a_silent_no_op(
    client: TestClient,
) -> None:
    response = client.delete(
        f"/api/v1/account?confirm={ACCOUNT_DELETE_CONFIRM}"
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


def test_a_confirmed_delete_erases_the_user_and_every_session(
    client: TestClient, state_db_path: Path
) -> None:
    _sign_in(client, state_db_path)
    assert _rows(state_db_path, "users") == 1
    assert _rows(state_db_path, "auth_sessions") == 1

    response = client.delete(
        f"/api/v1/account?confirm={ACCOUNT_DELETE_CONFIRM}"
    )
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    # The FK cascade is the point: the Google refresh and access tokens live
    # on the session rows, so an orphaned session would outlive the account.
    assert _rows(state_db_path, "users") == 0
    assert _rows(state_db_path, "auth_sessions") == 0


def test_the_deleted_session_no_longer_resolves(
    client: TestClient, state_db_path: Path
) -> None:
    _sign_in(client, state_db_path)
    client.delete(f"/api/v1/account?confirm={ACCOUNT_DELETE_CONFIRM}")
    assert client.get("/api/v1/account").json()["signed_in"] is False


# ----- ENT-02 / ENT-04: entitlements over the wire -------------------------
def test_entitlements_carry_the_whole_refusal_shape(client: TestClient) -> None:
    body = client.get("/api/v1/entitlements").json()
    assert body["refusal"]["code"] == NOT_IN_PLAN_CODE
    assert body["refusal"]["message"] == NOT_IN_PLAN_MESSAGE
    assert body["refusal"]["ui_title"] == UI_REFUSAL_TITLE
    assert body["provider"] is None


def test_nothing_is_gated_so_the_feature_list_is_empty(
    client: TestClient,
) -> None:
    assert client.get("/api/v1/entitlements").json()["features"] == []


@pytest.mark.parametrize(
    "feature_id", ["cloudsync.hosted-storage", "anything.at.all", "pro"]
)
def test_an_unknown_feature_is_entitled_and_unlimited(
    client: TestClient, feature_id: str
) -> None:
    body = client.get(f"/api/v1/entitlements/{feature_id}").json()
    assert body["feature_id"] == feature_id
    assert body["entitled"] is True
    # None is unlimited. A 404 here would let a caller read "never heard of
    # it" as "you may not".
    assert body["quota"] is None


def test_a_configured_provider_fails_the_request_loudly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No provider is implemented, so naming one must stop, not degrade."""
    monkeypatch.setenv(PROVIDER_ENV, "polar")
    with pytest.raises(Exception) as excinfo:
        client.get("/api/v1/entitlements")
    assert PROVIDER_ENV in str(excinfo.value)


# ----- FLAG-01 / FLAG-03: flags are their own surface ----------------------
def test_flags_report_the_file_and_the_source_of_each_value(
    client: TestClient, data_dir: Path
) -> None:
    body = client.get("/api/v1/flags").json()
    assert body["path"].endswith(FLAGS_FILENAME)
    assert body["file_present"] is False
    assert [flag["flag_id"] for flag in body["flags"]] == [
        "example.off_by_default"
    ]
    assert body["flags"][0]["enabled"] is False
    assert body["flags"][0]["overridden"] is False


def test_flags_are_read_at_startup_not_per_request(
    client: TestClient, data_dir: Path
) -> None:
    """Writing the file behind a running app must not change the answer.

    A flag is a deploy-time decision. Re-reading per request would make it a
    runtime one and let a half-written file change behaviour mid-request.
    """
    before = client.get("/api/v1/flags").json()
    (data_dir / FLAGS_FILENAME).write_text(
        json.dumps({"example.off_by_default": True}), encoding="utf-8"
    )
    assert client.get("/api/v1/flags").json() == before


# ----- SAND-01: the fourth refusal, on the wire ---------------------------
def _store_build_client(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, state_db_path: Path
) -> Iterator[TestClient]:
    """A client booted the way a packaged App Store build boots.

    The REAL flag registry and the REAL shipped profile, not a fixture pair:
    what is under test is that the file this project actually ships produces
    the refusal, so substituting a convenient one would test the substitute.
    """
    monkeypatch.setenv(BUILD_PROFILE_ENV, STORE_PROFILE)
    app = FastAPI()
    app.include_router(flags_router, prefix="/api/v1")
    app.state.state_db_path = str(state_db_path)
    app.state.feature_flags = load_flags(data_dir)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def store_build_client(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, state_db_path: Path
) -> Iterator[TestClient]:
    yield from _store_build_client(monkeypatch, data_dir, state_db_path)


def _full_build_client(
    data_dir: Path, state_db_path: Path
) -> Iterator[TestClient]:
    """The ordinary (non-store) build, with the REAL flag registry.

    Thread-2/SAND-04 is about the real ``usb.export`` flag under a
    mis-packaged bundle, not the module's synthetic ``TEST_DEFS`` flag, so
    this reads the same registry ``store_build_client`` does without
    selecting the store profile.
    """
    app = FastAPI()
    app.include_router(flags_router, prefix="/api/v1")
    app.state.state_db_path = str(state_db_path)
    app.state.feature_flags = load_flags(data_dir)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def full_build_client(
    data_dir: Path, state_db_path: Path
) -> Iterator[TestClient]:
    yield from _full_build_client(data_dir, state_db_path)


def test_flags_names_the_build_and_the_container(client: TestClient) -> None:
    """SAND-04: the profile is a CONFIG fact, the container a RUNTIME one.

    Reported side by side rather than folded together because they can
    disagree, and noticing when they do is the point: a mis-packaged bundle is
    sandboxed on the full profile, and a developer can select the store
    profile without being sandboxed at all.
    """
    body = client.get("/api/v1/flags").json()
    assert body["build_profile"] == DEFAULT_PROFILE
    assert body["sandboxed"] is False


def test_a_flag_the_store_profile_turned_off_carries_the_fourth_refusal(
    store_build_client: TestClient,
) -> None:
    body = store_build_client.get("/api/v1/flags").json()
    assert body["build_profile"] == STORE_PROFILE
    usb = next(f for f in body["flags"] if f["flag_id"] == "usb.export")
    assert usb["enabled"] is False
    assert usb["overridden"] is True
    assert usb["refusal"] is not None, (
        "the store build turned this flag off and said nothing about why, so "
        "a dead control has no fourth sentence to render"
    )
    assert usb["refusal"]["code"] == STORE_BUILD_REFUSAL_CODE
    assert usb["refusal"]["ui_title"] == STORE_BUILD_REFUSAL_TITLE
    # ENT-02's rule, applied to the fourth state: the tooltip and the message
    # are the same fact, so neither may be composed at the far end.
    assert "usb.export" in usb["refusal"]["message"]
    assert usb["refusal"]["ui_title"] != UI_REFUSAL_TITLE


def test_a_full_build_refuses_nothing_even_when_a_flag_is_off(
    client: TestClient,
) -> None:
    """The control, and the mutation that matters most.

    ``example.off_by_default`` is OFF here, exactly like usb.export is in the
    store build. If the route keyed the refusal on "the flag is off" it would
    pass the test above and fail this one, telling a developer that Apple
    removed a flag they switched off themselves. That is the third state's
    mistake repeated one state later.
    """
    body = client.get("/api/v1/flags").json()
    off = next(f for f in body["flags"] if f["flag_id"] == "example.off_by_default")
    assert off["enabled"] is False
    assert off["refusal"] is None


def test_an_explicit_flag_file_is_not_a_store_build(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, state_db_path: Path
) -> None:
    """MDT_FEATURE_FLAGS_FILE beats a named profile, so the values did not
    come from the shipped store profile even though the name is still set.

    Without the path check this returns the App Store refusal for a lane's
    scratch file, which is a sentence about Apple attached to a build Apple
    has never seen.
    """
    scratch = data_dir / "lane-flags.json"
    scratch.write_text(json.dumps({"usb.export": False}), encoding="utf-8")
    monkeypatch.setenv(BUILD_PROFILE_ENV, STORE_PROFILE)
    monkeypatch.setenv("MDT_FEATURE_FLAGS_FILE", str(scratch))

    app = FastAPI()
    app.include_router(flags_router, prefix="/api/v1")
    app.state.feature_flags = load_flags(data_dir)
    with TestClient(app) as scratch_client:
        body = scratch_client.get("/api/v1/flags").json()

    usb = next(f for f in body["flags"] if f["flag_id"] == "usb.export")
    assert usb["enabled"] is False
    assert usb["refusal"] is None, (
        "flag values came from an explicit lane file, not the shipped store "
        "profile, so nothing here is Apple's doing"
    )


def test_a_mispackaged_full_build_still_refuses_when_actually_sandboxed(
    full_build_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SAND-04/Thread-2: a mis-packaged bundle can ship the FULL profile
    while still running inside Apple's sandbox. The flag then resolves
    "enabled" -- nothing overrode it -- so a refusal derived from
    flag.enabled alone would tell the panel every capability is available
    while the daemon's own USB routes 503 underneath it (a real fail-open
    on the disclosure surface).
    """
    import apps.engine_core.account.api as account_api

    monkeypatch.setattr(account_api, "is_sandboxed", lambda: True)
    body = full_build_client.get("/api/v1/flags").json()
    assert body["build_profile"] == DEFAULT_PROFILE
    assert body["sandboxed"] is True
    usb = next(f for f in body["flags"] if f["flag_id"] == "usb.export")
    assert usb["enabled"] is True, "the full profile put no override on this flag"
    assert usb["refusal"] is not None, (
        "the process is genuinely sandboxed right now, so the capability "
        "cannot work regardless of what the flag says"
    )
    assert usb["refusal"]["code"] == STORE_BUILD_REFUSAL_CODE
    assert usb["refusal"]["ui_title"] == STORE_BUILD_REFUSAL_TITLE
    assert "usb.export" in usb["refusal"]["message"]


def test_a_full_build_that_is_not_sandboxed_refuses_nothing(
    full_build_client: TestClient,
) -> None:
    """The control for the mutation above: an ordinary, correctly packaged
    full build (not sandboxed) must not carry the fourth refusal just
    because it shares a flag store with the store profile's registry.
    """
    body = full_build_client.get("/api/v1/flags").json()
    assert body["sandboxed"] is False
    usb = next(f for f in body["flags"] if f["flag_id"] == "usb.export")
    assert usb["enabled"] is True
    assert usb["refusal"] is None


def test_flags_and_entitlements_are_separate_responses(
    client: TestClient,
) -> None:
    """FLAG-01 at the HTTP boundary: one payload would be one store."""
    entitlement_body = client.get("/api/v1/entitlements").json()
    flag_body = client.get("/api/v1/flags").json()
    assert "flags" not in entitlement_body
    assert "plan" not in flag_body
    assert "features" not in flag_body
