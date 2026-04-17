"""Tests for apps.cloud.config (CAT-04)."""
from __future__ import annotations

import pytest

from apps.cloud.config import REQUIRED_VARS, CloudConfig, MissingEnvError


@pytest.mark.requirement("CAT-04")
def test_from_env_happy_path():
    env = {
        "R2_ACCOUNT_ID": "acct-xyz",
        "R2_ACCESS_KEY_ID": "akid",
        "R2_SECRET_ACCESS_KEY": "secret",
        "MUSIC_DJ_STATE_BUCKET": "bucket-state",
        "MUSIC_DJ_AUDIO_BUCKET": "bucket-audio",
        "MUSIC_DJ_HOSTNAME": "mbp",
        "MUSIC_DJ_BIND_HOST": "127.0.0.1",
    }
    cfg = CloudConfig.from_env(env)
    assert cfg.r2_account_id == "acct-xyz"
    assert cfg.state_bucket == "bucket-state"
    assert cfg.audio_bucket == "bucket-audio"
    assert cfg.hostname == "mbp"
    assert cfg.bind_host == "127.0.0.1"
    assert cfg.r2_endpoint == "https://acct-xyz.r2.cloudflarestorage.com"


@pytest.mark.requirement("CAT-04")
def test_from_env_defaults_are_applied():
    env = {
        "R2_ACCOUNT_ID": "a",
        "R2_ACCESS_KEY_ID": "b",
        "R2_SECRET_ACCESS_KEY": "c",
    }
    cfg = CloudConfig.from_env(env)
    assert cfg.state_bucket == "music-dj-state"
    assert cfg.audio_bucket == "music-dj-audio"
    assert cfg.bind_host == "127.0.0.1"
    assert cfg.hostname  # whatever socket.gethostname returns


@pytest.mark.requirement("CAT-04")
@pytest.mark.parametrize("missing", REQUIRED_VARS)
def test_from_env_raises_on_missing(missing: str):
    env = {v: "x" for v in REQUIRED_VARS}
    del env[missing]
    with pytest.raises(MissingEnvError) as excinfo:
        CloudConfig.from_env(env)
    assert missing in str(excinfo.value)


@pytest.mark.requirement("CAT-04")
def test_from_env_raises_on_empty_value():
    env = {v: "x" for v in REQUIRED_VARS}
    env["R2_ACCOUNT_ID"] = ""
    with pytest.raises(MissingEnvError):
        CloudConfig.from_env(env)


@pytest.mark.requirement("CAT-04")
def test_repr_masks_secret_access_key():
    """Regression for SECURITY-RED-TEAM finding 3 (MEDIUM).

    The default dataclass repr would include the secret verbatim; any
    logging or exception path that stringifies the config must not leak.
    """
    secret = "AKIA-super-secret-do-not-leak-this-value"
    env = {
        "R2_ACCOUNT_ID": "acct-xyz",
        "R2_ACCESS_KEY_ID": "AKIATESTIDHERE",
        "R2_SECRET_ACCESS_KEY": secret,
    }
    cfg = CloudConfig.from_env(env)
    rendered = repr(cfg)
    assert secret not in rendered
    assert "AKIATESTIDHERE" not in rendered
    assert "***" in rendered
    # And str() goes through the same path.
    assert secret not in str(cfg)
    assert secret not in f"{cfg}"
    # f-string with !r must also be masked.
    assert secret not in f"{cfg!r}"
