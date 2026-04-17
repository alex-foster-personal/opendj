"""Shared fixtures for apps/voice tests."""
from __future__ import annotations

import pytest

from apps.voice import bus as bus_mod
from apps.voice import context as ctx_mod
from apps.voice import tts as tts_mod


@pytest.fixture(autouse=True)
def _reset_bus_warning():
    """Reset the one-shot stub-bus warning latch between tests."""
    bus_mod.reset_warning()
    yield
    bus_mod.reset_warning()


@pytest.fixture
def in_memory_bus() -> bus_mod.InMemoryBus:
    return bus_mod.InMemoryBus()


@pytest.fixture
def recording_tts() -> tts_mod.RecordingTts:
    return tts_mod.RecordingTts()


@pytest.fixture
def voice_context(in_memory_bus, recording_tts):
    return ctx_mod.VoiceContext(
        event_bus=in_memory_bus,
        tts_engine=recording_tts,
    )
