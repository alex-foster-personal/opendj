"""The auto-update channel: what it offers, and how it fails.

Single-line acceptance checks, in the repo's "if X then broken" shape:

- if an unreachable, refused or malformed endpoint reports up-to-date, an
  outage is rendered to the user as reassurance -> broken.
- if the check answers 200 on a fault, the next caller who only reads the
  status code treats a broken channel as a working one -> broken.
- if this module's endpoint and the shell's tauri.conf.json endpoint drift,
  the button and the agent CLI check different channels -> broken.
- if a manifest with no entry for this platform still reports a version, the
  app offers an update it cannot install -> broken.
- if a platform entry with no signature is accepted, the check advertises an
  update the updater will refuse at install time -> broken.
- if equal semver with a different build reports a bare up-to-date, the
  stale-artifact afternoon build_info exists to end happens again -> broken.
- if the CLI exits 0 on a fault, a script mistakes an outage for up-to-date
  -> broken.
- if a version string that is not semver is ordered anyway, the channel
  guesses at which build is newer -> broken.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.build_info import (
    BUILD_IDENTITY_STATE_ATTR,
    BuildIdentity,
    BuildInfoOut,
)
from apps.engine_core.update_channel import (
    UPDATE_CHECK_PATH,
    UPDATE_ENDPOINT,
    UpdateCheckError,
    _apply_via_engine,
    add_update_check_route,
    compare_versions,
    parse_version,
    platform_key,
    resolve_update_check,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
TAURI_CONF: Path = REPO_ROOT / "apps/desktop/src-tauri/tauri.conf.json"

RUNNING_VERSION: str = json.loads(TAURI_CONF.read_text(encoding="utf-8"))["version"]
RUNNING_SHA_FULL: str = "0d41a28c0000000000000000000000000000beef"
KEY: str = "darwin-aarch64"


def _identity(app_version: str | None = RUNNING_VERSION) -> BuildIdentity:
    return BuildIdentity(
        info=BuildInfoOut(
            source="payload",
            engine_version="0.1.0",
            git_sha="0d41a28c",
            git_sha_full=RUNNING_SHA_FULL,
            git_branch="main",
            git_dirty=False,
            built_at_utc="2026-08-31T12:00:00Z",
            built_at_kind="payload-build",
            app_version=app_version,
        ),
        failure=None,
    )


def _repo_identity(app_version: str | None = RUNNING_VERSION) -> BuildIdentity:
    return BuildIdentity(
        info=BuildInfoOut(
            source="repo",
            engine_version="0.1.0",
            git_sha="0d41a28c",
            git_sha_full=RUNNING_SHA_FULL,
            git_branch="main",
            git_dirty=False,
            built_at_utc="2026-08-31T12:00:00Z",
            built_at_kind="head-commit",
            app_version=app_version,
        ),
        failure=None,
    )


def _manifest(version: str, *, notes: str = "", key: str = KEY) -> dict[str, object]:
    return {
        "version": version,
        "notes": notes,
        "pub_date": "2026-09-01T00:00:00Z",
        "platforms": {
            key: {
                "signature": "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZw==",
                "url": f"https://example.invalid/OpenDJ-{version}.app.tar.gz",
            }
        },
    }


def _client(handler) -> httpx.Client:
    """An httpx client whose transport is a function, so no socket is opened."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _json_ok(body: object):
    return lambda request: httpx.Response(200, json=body)


# ----- the endpoint cannot drift from the shell ---------------------------
def test_endpoint_matches_the_shell_configuration() -> None:
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    endpoints = conf["plugins"]["updater"]["endpoints"]
    assert endpoints[0] == UPDATE_ENDPOINT, (
        "the engine check and the Tauri updater must read ONE channel; "
        f"conf={endpoints[0]!r} module={UPDATE_ENDPOINT!r}"
    )


def test_endpoint_uses_the_public_release_host() -> None:
    """An unauthenticated updater cannot read assets from the private repo."""
    assert UPDATE_ENDPOINT == (
        "https://github.com/maintainer/issue-assets"
        "/releases/latest/download/latest.json"
    )


def test_the_shell_carries_a_public_key() -> None:
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    pubkey = conf["plugins"]["updater"]["pubkey"]
    assert isinstance(pubkey, str) and len(pubkey) > 40, (
        "without a pubkey the updater cannot verify a package and check() fails"
    )


def test_updater_artifacts_are_produced_by_the_bundler() -> None:
    # Without this the dmg builds but no .app.tar.gz exists to serve, so the
    # channel could only ever offer a package that was never created.
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    assert conf["bundle"]["createUpdaterArtifacts"] is True


# ----- semver -------------------------------------------------------------
def test_a_higher_channel_version_is_an_update() -> None:
    assert compare_versions("0.1.0", "0.2.0") == "update-available"


def test_an_equal_version_is_up_to_date() -> None:
    assert compare_versions("0.1.0", "0.1.0") == "up-to-date"


def test_a_build_newer_than_the_channel_says_so() -> None:
    assert compare_versions("0.9.0", "0.1.0") == "ahead-of-channel"


def test_a_leading_v_is_tolerated() -> None:
    assert compare_versions("0.1.0", "v0.2.0") == "update-available"


def test_an_unorderable_version_is_refused_rather_than_guessed() -> None:
    with pytest.raises(UpdateCheckError) as caught:
        parse_version("nightly", "the channel manifest's 'version'")
    assert caught.value.status == "manifest-malformed"
    assert "nightly" in caught.value.message


# ----- platform key -------------------------------------------------------
def test_the_platform_key_is_tauris_os_arch_shape() -> None:
    assert platform_key("Darwin", "arm64") == "darwin-aarch64"
    assert platform_key("Windows", "AMD64") == "windows-x86_64"


def test_an_unmappable_platform_is_refused() -> None:
    with pytest.raises(UpdateCheckError) as caught:
        platform_key("Plan9", "risc-v")
    assert caught.value.status == "platform-unsupported"


# ----- the happy path -----------------------------------------------------
def test_a_newer_release_is_offered_with_both_versions_named() -> None:
    with _client(_json_ok(_manifest("0.2.0"))) as client:
        result = resolve_update_check(_identity(), client=client, key=KEY)
    assert result.status == "update-available"
    assert result.current_version == RUNNING_VERSION
    assert result.available_version == "0.2.0"
    assert result.current_git_sha == "0d41a28c"
    assert "0.2.0" in (result.detail or "")


def test_the_same_release_is_up_to_date() -> None:
    notes = f"built from {RUNNING_SHA_FULL}"
    with _client(_json_ok(_manifest(RUNNING_VERSION, notes=notes))) as client:
        result = resolve_update_check(_identity(), client=client, key=KEY)
    assert result.status == "up-to-date"
    assert result.same_version_different_build is False


def test_the_same_version_from_a_different_build_is_surfaced() -> None:
    # The case semver cannot see. The updater will not act; the human is told.
    with _client(
        _json_ok(_manifest(RUNNING_VERSION, notes="built from cafe1234"))
    ) as client:
        result = resolve_update_check(_identity(), client=client, key=KEY)
    assert result.status == "up-to-date"
    assert result.same_version_different_build is True


# ----- every fault is named, and none of them says up-to-date -------------
def test_an_unreachable_endpoint_is_a_named_fault() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with _client(refuse) as client:
        result = resolve_update_check(_identity(), client=client, key=KEY)
    assert result.status == "endpoint-unreachable"
    assert result.detail is not None and UPDATE_ENDPOINT in result.detail


def test_a_404_is_refused_not_reassuring() -> None:
    with _client(lambda r: httpx.Response(404, text="Not Found")) as client:
        result = resolve_update_check(_identity(), client=client, key=KEY)
    assert result.status == "endpoint-refused"
    assert "404" in (result.detail or "")
    assert "public release host" in (result.detail or "")


def test_a_non_json_body_is_malformed() -> None:
    with _client(lambda r: httpx.Response(200, text="<html>hi</html>")) as client:
        result = resolve_update_check(_identity(), client=client, key=KEY)
    assert result.status == "manifest-malformed"


def test_a_manifest_without_this_platform_is_unsupported() -> None:
    body = _manifest("0.2.0", key="windows-x86_64")
    with _client(_json_ok(body)) as client:
        result = resolve_update_check(_identity(), client=client, key=KEY)
    assert result.status == "platform-unsupported"
    assert "windows-x86_64" in (result.detail or "")


def test_a_platform_entry_without_a_signature_is_refused() -> None:
    body = _manifest("0.2.0")
    body["platforms"][KEY].pop("signature")  # type: ignore[index]
    with _client(_json_ok(body)) as client:
        result = resolve_update_check(_identity(), client=client, key=KEY)
    assert result.status == "manifest-malformed"
    assert "signature" in (result.detail or "")


def test_a_build_that_cannot_name_its_version_is_not_compared() -> None:
    with _client(_json_ok(_manifest("0.2.0"))) as client:
        result = resolve_update_check(_identity(app_version=None), client=client, key=KEY)
    assert result.status == "identity-unavailable"


def test_an_unidentified_build_is_not_compared() -> None:
    unresolved = BuildIdentity(info=None, failure="no manifest at /nope")
    with _client(_json_ok(_manifest("0.2.0"))) as client:
        result = resolve_update_check(unresolved, client=client, key=KEY)
    assert result.status == "identity-unavailable"
    assert "no manifest" in (result.detail or "")


# ----- the route ----------------------------------------------------------
def _app(identity: BuildIdentity) -> FastAPI:
    app = FastAPI()
    setattr(app.state, BUILD_IDENTITY_STATE_ATTR, identity)
    add_update_check_route(app)
    return app


def _route_response(monkeypatch, handler, *, identity: BuildIdentity | None = None):
    """Drive the mounted route with a transport that opens no socket.

    The mock client is built BEFORE httpx.Client is patched: patching first
    and constructing inside the patch makes the helper call its own stub.
    """
    import apps.engine_core.update_channel as module

    mock_client = _client(handler)
    monkeypatch.setattr(module.httpx, "Client", lambda: mock_client)
    monkeypatch.setattr(module, "platform_key", lambda *a, **k: KEY)
    resolved = identity if identity is not None else _identity()
    with TestClient(_app(resolved)) as client:
        return client.get(UPDATE_CHECK_PATH)


def test_the_route_answers_200_when_the_channel_answered(monkeypatch) -> None:
    response = _route_response(monkeypatch, _json_ok(_manifest(RUNNING_VERSION)))
    assert response.status_code == 200
    assert response.json()["status"] == "up-to-date"


def test_the_route_answers_502_on_a_payload_fault(monkeypatch) -> None:
    response = _route_response(
        monkeypatch, lambda r: httpx.Response(404, text="nope")
    )
    # A fault is an HTTP fault. 200-with-a-sad-field is how a broken channel
    # gets read as a working one.
    assert response.status_code == 502
    assert response.json()["status"] == "endpoint-refused"


def test_a_repo_checkout_returns_200_when_the_channel_faults(monkeypatch) -> None:
    response = _route_response(
        monkeypatch,
        lambda r: httpx.Response(404, text="nope"),
        identity=_repo_identity(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "endpoint-refused"
    assert "error" not in body


# ----- the agent-native path ----------------------------------------------
def test_the_cli_exists_and_refuses_an_unreachable_channel() -> None:
    # Agent parity: the same question the UI button asks, over a CLI, and a
    # NON-ZERO exit so a script cannot read an outage as up-to-date.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.engine_core.update_channel",
            "check",
            "--endpoint",
            "http://127.0.0.1:9/latest.json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 2, (
        f"expected a non-zero exit on an unreachable channel; "
        f"stdout={result.stdout} stderr={result.stderr}"
    )
    payload = json.loads(result.stdout)
    assert payload["status"] in {"endpoint-unreachable", "identity-unavailable"}


def _apply_http_handler(calls: dict[str, int] | None = None):
    state = calls if calls is not None else {"build_info": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/build-info"):
            state["build_info"] = state.get("build_info", 0) + 1
            first = state["build_info"] == 1
            sha = RUNNING_SHA_FULL if first else "deadbeef0000000000000000000000000000beef"
            return httpx.Response(
                200,
                json={
                    "source": "payload",
                    "engine_version": "0.1.0",
                    "git_sha": sha[:8],
                    "git_sha_full": sha,
                    "git_branch": "main",
                    "git_dirty": False,
                    "built_at_utc": "2026-08-31T12:00:00Z",
                    "built_at_kind": "payload-build",
                    "app_version": RUNNING_VERSION if first else "0.1.1",
                },
            )
        if request.url.path.endswith("/update/check"):
            status = state.get("check_status", "update-available")
            return httpx.Response(200, json={"status": status, "available_version": "0.1.1"})
        if request.url.path.endswith("/update/apply"):
            return httpx.Response(
                202,
                json={
                    "accepted": True,
                    "available_version": "0.1.1",
                    "command_id": "abc123",
                },
            )
        raise AssertionError(f"unexpected URL {request.url}")

    return handler


def _mock_client_factory(module, transport: httpx.MockTransport):
    original = module.httpx.Client

    def client_factory(*args, **kwargs):
        if not args and not kwargs:
            return httpx.Client(transport=transport)
        return original(*args, **kwargs)

    return client_factory


def test_apply_cli_refuses_when_check_is_not_update_available(monkeypatch) -> None:
    import apps.engine_core.update_channel as module

    transport = httpx.MockTransport(
        _apply_http_handler({"build_info": 0, "check_status": "up-to-date"})
    )
    monkeypatch.setattr(module.httpx, "Client", _mock_client_factory(module, transport))
    assert _apply_via_engine("http://127.0.0.1:8685", 1.0) == 2


def test_apply_cli_exits_zero_when_build_identity_changes(monkeypatch) -> None:
    import apps.engine_core.update_channel as module

    transport = httpx.MockTransport(_apply_http_handler())
    monkeypatch.setattr(module.httpx, "Client", _mock_client_factory(module, transport))
    monkeypatch.setattr(
        module,
        "resolve_origin",
        lambda *a, **k: type("O", (), {"base_url": "http://127.0.0.1:8685"})(),
    )
    assert _apply_via_engine("http://127.0.0.1:8685", 5.0) == 0
