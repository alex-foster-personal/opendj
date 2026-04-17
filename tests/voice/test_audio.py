"""Unit tests for apps.voice.audio (VOICE-01)."""
from __future__ import annotations

import pytest

from apps.voice import audio


pytestmark = pytest.mark.requirement("VOICE-01")


class TestIsBuiltinMic:
    def test_matches_macbook(self):
        assert audio.is_builtin_mic("MacBook Pro Microphone") is True

    def test_matches_builtin_variants(self):
        assert audio.is_builtin_mic("Built-in Microphone") is True
        assert audio.is_builtin_mic("BuiltIn Audio") is True
        assert audio.is_builtin_mic("Internal Input") is True

    def test_rejects_external_mic(self):
        assert audio.is_builtin_mic("Shure WH20") is False
        assert audio.is_builtin_mic("AKG C520") is False


class TestResolveInputDevice:
    def test_env_override_used_when_set(self):
        calls: list[tuple[int, str]] = []

        def fake_query(idx, kind):
            calls.append((idx, kind))
            return {"name": "MockMic", "default_samplerate": 48_000, "max_input_channels": 2}

        def fake_default():
            return (99, 99)

        info = audio.resolve_input_device(
            env={"VOICE_INPUT_DEVICE": "3"},
            query_devices=fake_query,
            default_device_getter=fake_default,
        )
        assert info.index == 3
        assert info.name == "MockMic"
        assert info.sample_rate_hz == 48_000
        assert calls == [(3, "input")]

    def test_falls_back_to_default(self):
        info = audio.resolve_input_device(
            env={},
            query_devices=lambda idx, kind: {"name": "Default", "default_samplerate": 16_000},
            default_device_getter=lambda: (7, 8),
        )
        assert info.index == 7
        assert info.name == "Default"

    def test_invalid_env_raises(self):
        with pytest.raises(ValueError):
            audio.resolve_input_device(
                env={"VOICE_INPUT_DEVICE": "not-an-int"},
                query_devices=lambda *a, **k: {},
                default_device_getter=lambda: (0, 0),
            )

    def test_no_device_resolvable_raises(self):
        with pytest.raises(RuntimeError, match="No input device"):
            audio.resolve_input_device(
                env={},
                query_devices=lambda *a, **k: {},
                default_device_getter=lambda: (None, None),
            )


class TestWarnIfBuiltin:
    def test_warns_on_builtin(self):
        msgs: list[str] = []
        audio.warn_if_builtin(
            audio.DeviceInfo(index=0, name="MacBook Air Microphone", sample_rate_hz=16_000, max_input_channels=1),
            warn=msgs.append,
        )
        assert any("built-in" in m.lower() or "laptop" in m.lower() for m in msgs)

    def test_silent_on_external(self):
        msgs: list[str] = []
        audio.warn_if_builtin(
            audio.DeviceInfo(index=0, name="Shure WH20", sample_rate_hz=16_000, max_input_channels=1),
            warn=msgs.append,
        )
        assert msgs == []


class TestRingBuffer:
    def test_fills_and_drops_oldest(self):
        rb = audio.RingBuffer(capacity_frames=3)
        rb.push(b"a")
        rb.push(b"b")
        rb.push(b"c")
        rb.push(b"d")
        assert len(rb) == 3
        assert rb.pop_all() == [b"b", b"c", b"d"]

    def test_pop_all_clears(self):
        rb = audio.RingBuffer(capacity_frames=2)
        rb.push(b"x")
        assert rb.pop_all() == [b"x"]
        assert len(rb) == 0

    def test_invalid_capacity(self):
        with pytest.raises(ValueError):
            audio.RingBuffer(capacity_frames=0)
