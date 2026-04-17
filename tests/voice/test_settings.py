"""Unit tests for apps.voice.settings (VOICE-01)."""
from __future__ import annotations

import pytest

from apps.voice import settings


pytestmark = pytest.mark.requirement("VOICE-01")


def test_set_get_roundtrip(tmp_path):
    s = settings.SettingsStore(path=tmp_path / "voice.sqlite")
    s.set("mute_until", 1234.5)
    assert s.get("mute_until") == "1234.5"


def test_get_float_typed(tmp_path):
    s = settings.SettingsStore(path=tmp_path / "voice.sqlite")
    s.set("mute_until", 9000.0)
    assert s.get_float("mute_until") == 9000.0


def test_get_int_typed(tmp_path):
    s = settings.SettingsStore(path=tmp_path / "voice.sqlite")
    s.set("tail_ms", 300)
    assert s.get_int("tail_ms") == 300


def test_get_missing_returns_default(tmp_path):
    s = settings.SettingsStore(path=tmp_path / "voice.sqlite")
    assert s.get("nonexistent") is None
    assert s.get("nonexistent", default="default") == "default"
    assert s.get_float("nonexistent", default=1.5) == 1.5


def test_delete(tmp_path):
    s = settings.SettingsStore(path=tmp_path / "voice.sqlite")
    s.set("foo", "bar")
    s.delete("foo")
    assert s.get("foo") is None


def test_all(tmp_path):
    s = settings.SettingsStore(path=tmp_path / "voice.sqlite")
    s.set("a", 1)
    s.set("b", 2)
    assert s.all() == {"a": "1", "b": "2"}


def test_upsert_replaces_existing(tmp_path):
    s = settings.SettingsStore(path=tmp_path / "voice.sqlite")
    s.set("mute_until", 1000)
    s.set("mute_until", 2000)
    assert s.get_float("mute_until") == 2000.0


def test_persists_across_instances(tmp_path):
    path = tmp_path / "voice.sqlite"
    s1 = settings.SettingsStore(path=path)
    s1.set("mute_until", 5000.0)
    del s1
    s2 = settings.SettingsStore(path=path)
    assert s2.get_float("mute_until") == 5000.0


def test_load_and_save_mute_until_helpers(tmp_path):
    s = settings.SettingsStore(path=tmp_path / "voice.sqlite")
    assert settings.load_mute_until(s) is None
    settings.save_mute_until(s, 1234.5)
    assert settings.load_mute_until(s) == 1234.5
    settings.save_mute_until(s, None)
    assert settings.load_mute_until(s) is None


def test_malformed_float_falls_back(tmp_path):
    s = settings.SettingsStore(path=tmp_path / "voice.sqlite")
    s.set("x", "not-a-number")
    assert s.get_float("x", default=99.0) == 99.0
    assert s.get_int("x", default=42) == 42
