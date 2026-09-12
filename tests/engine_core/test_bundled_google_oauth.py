"""AUTH-01: the engine reads a baked Desktop-app Google OAuth client.

A packaged install has no Doppler and no shell env. GoogleOAuthConfig must
load the client from a bundled JSON file (explicit path, sidecar, or
payload-manifest sibling) so health can report google_oauth_configured
true. Process env still wins: a tester's Doppler shell must not be
overwritten by the bake.

Regression lines:
  - if from_env({}) with a bundled file still raises, packaged sign-in is
    dead on every install
  - if a process env client is replaced by the bundled file, a Doppler
    override cannot be tested
  - if apply_bundled_oauth skips blank keys, boot never publishes the
    baked client to os.environ for other readers
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.google_oauth_client import apply_bundled_oauth
from apps.webui.server.auth import AuthConfigError, GoogleOAuthConfig

pytestmark = pytest.mark.requirement("AUTH-01")


def _write_bundled(path: Path, *, client_id: str, client_secret: str) -> Path:
    path.write_text(
        json.dumps({"client_id": client_id, "client_secret": client_secret}),
        encoding="utf-8",
    )
    return path


def test_bundled_file_is_read_when_env_has_no_client(tmp_path: Path) -> None:
    path = _write_bundled(
        tmp_path / "bundled_google_oauth.json",
        client_id="baked-desktop-id",
        client_secret="baked-desktop-secret",
    )
    env = {"OPENDJ_GOOGLE_OAUTH_CONFIG": str(path)}
    config = GoogleOAuthConfig.from_env(env)
    assert config.client_id == "baked-desktop-id"
    assert config.client_secret == "baked-desktop-secret"
    assert GoogleOAuthConfig.is_configured(env) is True


def test_process_env_wins_over_bundled_file(tmp_path: Path) -> None:
    path = _write_bundled(
        tmp_path / "bundled_google_oauth.json",
        client_id="baked-id",
        client_secret="baked-secret",
    )
    config = GoogleOAuthConfig.from_env(
        {
            "OPENDJ_GOOGLE_OAUTH_CONFIG": str(path),
            "OPENDJ_GOOGLE_OAUTH_CLIENT_ID": "env-id",
            "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET": "env-secret",
        }
    )
    assert config.client_id == "env-id"
    assert config.client_secret == "env-secret"


def test_apply_bundled_oauth_fills_blank_environ(tmp_path: Path) -> None:
    path = _write_bundled(
        tmp_path / "bundled_google_oauth.json",
        client_id="baked-desktop-id",
        client_secret="baked-desktop-secret",
    )
    environ = {"OPENDJ_GOOGLE_OAUTH_CONFIG": str(path)}
    apply_bundled_oauth(environ)
    assert environ["OPENDJ_GOOGLE_OAUTH_CLIENT_ID"] == "baked-desktop-id"
    assert environ["OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET"] == "baked-desktop-secret"


def test_apply_bundled_oauth_does_not_overwrite_present_env(tmp_path: Path) -> None:
    path = _write_bundled(
        tmp_path / "bundled_google_oauth.json",
        client_id="baked-id",
        client_secret="baked-secret",
    )
    environ = {
        "OPENDJ_GOOGLE_OAUTH_CONFIG": str(path),
        "OPENDJ_GOOGLE_OAUTH_CLIENT_ID": "env-id",
        "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET": "env-secret",
    }
    apply_bundled_oauth(environ)
    assert environ["OPENDJ_GOOGLE_OAUTH_CLIENT_ID"] == "env-id"
    assert environ["OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET"] == "env-secret"


def test_manifest_relative_config_is_read(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    (payload / "config").mkdir(parents=True)
    (payload / "manifest.json").write_text("{}", encoding="utf-8")
    _write_bundled(
        payload / "config" / "google_oauth.json",
        client_id="manifest-id",
        client_secret="manifest-secret",
    )
    config = GoogleOAuthConfig.from_env(
        {"OPENDJ_PAYLOAD_MANIFEST": str(payload / "manifest.json")}
    )
    assert config.client_id == "manifest-id"
    assert config.client_secret == "manifest-secret"


def test_empty_env_without_a_bundled_file_still_raises() -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        GoogleOAuthConfig.from_env({})
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_ID" in str(excinfo.value)
