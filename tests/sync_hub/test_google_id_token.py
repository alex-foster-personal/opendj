"""Google OAuth client-id precedence, shared by sign-in and the hub verifier.

[if] the hub and sign-in pick different client-id env vars, or a missing id
stops failing loudly [then] fail, [else stop].
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from apps.shared import google_oauth_client
from apps.shared.google_oauth_client import CLIENT_ID_ENV_NAMES, CLIENT_SECRET_ENV_NAMES, first_present
from apps.sync_hub import google_id_token
from apps.sync_hub.google_id_token import GoogleIdTokenConfig, GoogleIdTokenVerifierUnavailable
from apps.webui.server.auth import AuthConfigError, GoogleOAuthConfig

_SECRETS: dict[str, str] = {CLIENT_SECRET_ENV_NAMES[0]: "secret"}


def _hub_client_id(env: dict[str, str]) -> str:
    """The audience the hub verifier pins for ``env``."""
    return GoogleIdTokenConfig.from_env(env).client_id


def _sign_in_client_id(env: dict[str, str]) -> str:
    """The client id web sign-in uses for ``env`` (with a secret supplied)."""
    return GoogleOAuthConfig.from_env({**_SECRETS, **env}).client_id


_CALLERS = pytest.mark.parametrize("client_id_for", [_hub_client_id, _sign_in_client_id])


def test_hub_verifier_uses_the_shared_helper() -> None:
    """if the hub carries its own copy of the precedence helper then broken"""
    assert google_id_token.first_present is google_oauth_client.first_present
    assert google_id_token.CLIENT_ID_ENV_NAMES is google_oauth_client.CLIENT_ID_ENV_NAMES


@_CALLERS
def test_first_configured_client_id_wins(client_id_for: Callable[[dict[str, str]], str]) -> None:
    """if the first configured client-id name is set then it wins over later names"""
    env = {CLIENT_ID_ENV_NAMES[0]: "specific-id", CLIENT_ID_ENV_NAMES[1]: "shared-id"}
    assert client_id_for(env) == "specific-id"


@_CALLERS
def test_later_client_id_is_used_when_earlier_is_absent_or_blank(
    client_id_for: Callable[[dict[str, str]], str],
) -> None:
    """if only a later client-id name is set (earlier absent or blank) then the later one is used"""
    assert client_id_for({CLIENT_ID_ENV_NAMES[1]: "shared-id"}) == "shared-id"
    assert client_id_for({CLIENT_ID_ENV_NAMES[0]: "  ", CLIENT_ID_ENV_NAMES[1]: "shared-id"}) == "shared-id"


def test_no_client_id_keeps_each_callers_failure() -> None:
    """if no client-id name is set then the helper returns None and both callers still fail loudly"""
    blank = {CLIENT_ID_ENV_NAMES[0]: "", CLIENT_ID_ENV_NAMES[1]: "   "}
    assert first_present({}, CLIENT_ID_ENV_NAMES) is None
    assert first_present(blank, CLIENT_ID_ENV_NAMES) is None
    with pytest.raises(GoogleIdTokenVerifierUnavailable, match=CLIENT_ID_ENV_NAMES[0]):
        GoogleIdTokenConfig.from_env(blank)
    with pytest.raises(AuthConfigError, match=CLIENT_ID_ENV_NAMES[1]):
        GoogleOAuthConfig.from_env({**_SECRETS, **blank})
