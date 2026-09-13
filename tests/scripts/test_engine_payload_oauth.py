"""AUTH-01: payload bake + fail-loud verify for the Google OAuth client.

[if] a payload ships without a bundled Google client id [then] verify must fail naming OPENDJ_GOOGLE_OAUTH_CLIENT_ID, [else stop].

The packaged engine has had google_oauth_configured: false on every
install because nothing baked OPENDJ_GOOGLE_OAUTH_CLIENT_ID into the
payload. Bake writes a JSON file next to the staged google_oauth_client
module; verify fails naming that env var when the client id is absent,
the same way it gates runtime loads.

Regression lines:
  - if verify passes with no bundled client id, a silent empty payload
    ships and every install has dead Google sign-in
  - if bake writes the values into a tracked repo path, a Desktop-app
    secret lands in git
  - if the launcher does not export the bundled config path, a payload
    that staged the file still boots with an empty env
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.payload_google_oauth import (
    BUNDLED_RELATIVE,
    PayloadOAuthError,
    bake_google_oauth,
    verify_bundled_oauth,
)

pytestmark = pytest.mark.requirement("AUTH-01")

FAKE_ID = "test-desktop-client-id"
FAKE_SECRET = "test-desktop-client-secret"


def _empty_credential_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "OPENDJ_GOOGLE_OAUTH_CLIENT_ID",
        "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
    ):
        env.pop(name, None)
    return env


def test_bake_writes_bundled_json_from_env(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    path = bake_google_oauth(
        payload,
        env={
            "OPENDJ_GOOGLE_OAUTH_CLIENT_ID": FAKE_ID,
            "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET": FAKE_SECRET,
        },
    )
    assert path == payload / BUNDLED_RELATIVE
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["client_id"] == FAKE_ID
    assert data["client_secret"] == FAKE_SECRET
    verify_bundled_oauth(payload)


def test_verify_fails_loud_when_the_bundled_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(PayloadOAuthError) as excinfo:
        verify_bundled_oauth(tmp_path / "payload")
    message = str(excinfo.value)
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_ID" in message
    assert FAKE_ID not in message
    assert FAKE_SECRET not in message


def test_verify_fails_loud_when_the_client_id_is_blank(tmp_path: Path) -> None:
    path = tmp_path / "payload" / BUNDLED_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"client_id": "  ", "client_secret": FAKE_SECRET}),
        encoding="utf-8",
    )
    with pytest.raises(PayloadOAuthError) as excinfo:
        verify_bundled_oauth(tmp_path / "payload")
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_ID" in str(excinfo.value)


def test_bake_fails_naming_the_client_id_when_env_and_doppler_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENDJ_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(PayloadOAuthError) as excinfo:
        bake_google_oauth(tmp_path / "payload", env=_empty_credential_env())
    message = str(excinfo.value)
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_ID" in message


def test_launcher_exports_bundled_oauth_config_path() -> None:
    from scripts.build_engine_payload import LAUNCHER_TEMPLATE

    assert "OPENDJ_GOOGLE_OAUTH_CONFIG=" in LAUNCHER_TEMPLATE
    assert "bundled_google_oauth.json" in LAUNCHER_TEMPLATE
    assert "export OPENDJ_GOOGLE_OAUTH_CONFIG" in LAUNCHER_TEMPLATE
