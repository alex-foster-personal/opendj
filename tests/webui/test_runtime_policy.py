"""Runtime policy module tests (issue #284, POLICY-01)."""
from __future__ import annotations

import importlib
import sys

import pytest

from apps.adapters.rekordbox import config as rb_config


def _flatten(body: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for group in body["groups"]:
        for item in group["items"]:
            out[item["key"]] = item
    return out


@pytest.mark.requirement("POLICY-01")
def test_runtime_policy_shipped_defaults():
    """[if] runtime_policy imports [then] shipped defaults match 0.3/38400/30s, [else stop]."""
    from apps.shared import runtime_policy

    assert runtime_policy.HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO == 0.3
    assert runtime_policy.ANLZ_POINTS_DEFAULT == 38400
    assert runtime_policy.ANLZ_POINTS_MIN == 100
    assert runtime_policy.ANLZ_POINTS_MAX == 38400
    assert runtime_policy.FILE_EXISTS_TTL_S == 30.0
    assert rb_config.FILE_EXISTS_TTL_S == 30.0


@pytest.mark.requirement("POLICY-01")
def test_mostly_broken_playlist_rules():
    """[if] mostly_broken_playlist runs [then] fast-list and ratio rules hold, [else stop]."""
    from apps.shared.runtime_policy import mostly_broken_playlist

    assert mostly_broken_playlist(-1, 10) is False
    assert mostly_broken_playlist(0, 0) is True
    assert mostly_broken_playlist(1, 0) is False
    assert mostly_broken_playlist(29, 100) is True
    assert mostly_broken_playlist(30, 100) is False


@pytest.mark.requirement("POLICY-01")
def test_settings_exposes_runtime_policy_keys(client):
    """[if] GET /api/v1/settings [then] all five POLICY-01 keys publish defaults, [else stop]."""
    items = _flatten(client.get("/api/v1/settings").json())
    assert items["hide_broken_playlist_min_available_ratio"]["value"] == 0.3
    assert items["anlz_points_default"]["value"] == 38400
    assert items["anlz_points_min"]["value"] == 100
    assert items["anlz_points_max"]["value"] == 38400
    assert items["file_exists_ttl_s"]["value"] == 30.0


@pytest.mark.requirement("POLICY-01")
def test_file_exists_ttl_env_override(monkeypatch: pytest.MonkeyPatch):
    """[if] MDT_FILE_EXISTS_TTL_S is set [then] config and settings read 45s, [else stop]."""
    monkeypatch.setenv("MDT_FILE_EXISTS_TTL_S", "45")
    for name in ("apps.shared.runtime_policy", "apps.adapters.rekordbox.config"):
        sys.modules.pop(name, None)
    runtime_policy = importlib.import_module("apps.shared.runtime_policy")
    rb_config = importlib.import_module("apps.adapters.rekordbox.config")
    assert runtime_policy.FILE_EXISTS_TTL_S == 45.0
    assert rb_config.FILE_EXISTS_TTL_S == 45.0


@pytest.mark.requirement("POLICY-01")
def test_invalid_hide_broken_ratio_raises(monkeypatch: pytest.MonkeyPatch):
    """[if] hide-broken ratio env is >1 [then] import raises ValueError, [else stop]."""
    monkeypatch.setenv("MDT_HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO", "1.5")
    sys.modules.pop("apps.shared.runtime_policy", None)
    with pytest.raises(ValueError, match="MDT_HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO"):
        importlib.import_module("apps.shared.runtime_policy")


@pytest.mark.requirement("POLICY-01")
def test_invalid_file_exists_ttl_raises(monkeypatch: pytest.MonkeyPatch):
    """[if] file_exists TTL env is zero [then] import raises ValueError, [else stop]."""
    monkeypatch.setenv("MDT_FILE_EXISTS_TTL_S", "0")
    sys.modules.pop("apps.shared.runtime_policy", None)
    with pytest.raises(ValueError, match="MDT_FILE_EXISTS_TTL_S"):
        importlib.import_module("apps.shared.runtime_policy")
