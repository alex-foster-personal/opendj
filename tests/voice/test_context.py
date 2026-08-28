"""Unit tests for apps.voice.context (VOICE-01)."""

from __future__ import annotations

import pytest

from apps.voice import bus as bus_mod
from apps.voice import context as ctx_mod
from apps.voice import settings as settings_mod
from apps.voice import tts as tts_mod

pytestmark = pytest.mark.requirement("VOICE-01")


def _ctx(**overrides):
    return ctx_mod.VoiceContext(
        event_bus=bus_mod.InMemoryBus(),
        tts_engine=tts_mod.RecordingTts(),
        **overrides,
    )


class TestMute:
    def test_mute_sets_future_time(self):
        c = _ctx()
        c.mute(now=100.0)
        assert c.mute_until == 100.0 + c.mute_duration_s

    def test_is_muted_respects_clock(self):
        c = _ctx()
        c.mute_until = 1_000.0
        assert c.is_muted(now=500.0) is True
        assert c.is_muted(now=2_000.0) is False

    def test_unmute_clears(self):
        c = _ctx()
        c.mute_until = 1_000.0
        c.unmute()
        assert c.mute_until is None
        assert not c.is_muted(now=0.0)

    def test_mute_and_unmute_persist_across_contexts(self, tmp_path):
        store = settings_mod.SettingsStore(path=tmp_path / "voice.sqlite")
        first = _ctx(settings_store=store)
        first.mute(now=100.0)

        second = ctx_mod.VoiceContext.from_env(
            env={},
            event_bus=bus_mod.InMemoryBus(),
            tts_engine=tts_mod.RecordingTts(),
            settings_store=store,
        )
        assert second.mute_until == 100.0 + second.mute_duration_s

        second.unmute()
        third = ctx_mod.VoiceContext.from_env(
            env={},
            event_bus=bus_mod.InMemoryBus(),
            tts_engine=tts_mod.RecordingTts(),
            settings_store=store,
        )
        assert third.mute_until is None


class TestDebounce:
    def test_first_call_not_debounced(self):
        c = _ctx()
        assert c.debounced() is False

    def test_within_window_debounced(self):
        c = _ctx(debounce_s=2.0)
        c.mark_dispatch(now=100.0)
        assert c.debounced(now=101.0) is True

    def test_past_window_clear(self):
        c = _ctx(debounce_s=2.0)
        c.mark_dispatch(now=100.0)
        assert c.debounced(now=103.0) is False

    def test_last_dispatch_persists_across_contexts(self, tmp_path):
        store = settings_mod.SettingsStore(path=tmp_path / "voice.sqlite")
        first = _ctx(settings_store=store, debounce_s=5.0)
        first.mark_dispatch(now=100.0)

        second = ctx_mod.VoiceContext.from_env(
            env={"VOICE_DEBOUNCE_S": "5"},
            event_bus=bus_mod.InMemoryBus(),
            tts_engine=tts_mod.RecordingTts(),
            settings_store=store,
        )
        assert second.last_dispatch_at == 100.0
        assert second.debounced(now=101.0) is True


class TestFromEnv:
    def test_destructive_flag_from_env(self):
        c = ctx_mod.VoiceContext.from_env(
            env={"VOICE_ENABLE_DESTRUCTIVE": "yes"},
            event_bus=bus_mod.InMemoryBus(),
            tts_engine=tts_mod.RecordingTts(),
        )
        assert c.destructive is True

    def test_destructive_flag_off_by_default(self):
        c = ctx_mod.VoiceContext.from_env(
            env={},
            event_bus=bus_mod.InMemoryBus(),
            tts_engine=tts_mod.RecordingTts(),
        )
        assert c.destructive is False

    def test_debounce_override(self):
        c = ctx_mod.VoiceContext.from_env(
            env={"VOICE_DEBOUNCE_S": "5"},
            event_bus=bus_mod.InMemoryBus(),
            tts_engine=tts_mod.RecordingTts(),
        )
        assert c.debounce_s == 5.0

    def test_bad_numbers_fallback(self):
        c = ctx_mod.VoiceContext.from_env(
            env={"VOICE_DEBOUNCE_S": "abc", "VOICE_MUTE_DURATION_S": "xyz"},
            event_bus=bus_mod.InMemoryBus(),
            tts_engine=tts_mod.RecordingTts(),
        )
        assert c.debounce_s == ctx_mod.DEFAULT_DEBOUNCE_S
        assert c.mute_duration_s == ctx_mod.DEFAULT_MUTE_DURATION_S
