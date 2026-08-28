"""Unit tests for apps.voice.wake (VOICE-01)."""

from __future__ import annotations

import pytest

from apps.voice import wake

pytestmark = pytest.mark.requirement("VOICE-01")


class TestStubBackend:
    def test_exposes_native_frame_size(self):
        assert wake.StubBackend.frame_samples == 480

    def test_fires_once_by_default(self):
        b = wake.StubBackend(confidence=1.0, threshold=0.5)
        assert b.detect(b"x") == 1.0
        assert b.detect(b"x") == 0.0

    def test_always_fires_when_always_true(self):
        b = wake.StubBackend(confidence=0.9, always=True, threshold=0.5)
        for _ in range(5):
            assert b.detect(b"x") == 0.9

    def test_reset_rearms(self):
        b = wake.StubBackend(confidence=1.0)
        b.detect(b"x")
        assert b.detect(b"x") == 0.0
        b.reset()
        assert b.detect(b"x") == 1.0


class TestMakeBackend:
    def test_real_backends_expose_native_frame_sizes(self):
        assert wake.OpenWakeWordBackend.frame_samples == 1_280
        assert wake.PorcupineBackend.frame_samples == 512

    def test_stub_env_picks_stub(self):
        b = wake.make_backend(env={"WAKE_BACKEND": "stub"})
        assert isinstance(b, wake.StubBackend)

    def test_openwakeword_factory_forwards_model_path(self, monkeypatch):
        received: dict[str, object] = {}

        class _Backend:
            def __init__(self, model_path: str | None, threshold: float) -> None:
                received.update(model_path=model_path, threshold=threshold)

        monkeypatch.setattr(wake, "OpenWakeWordBackend", _Backend)

        wake.make_backend(
            env={
                "WAKE_BACKEND": "openwakeword",
                "VOICE_WAKE_MODEL_PATH": "models/hey-booth.onnx",
                "VOICE_WAKE_SENSITIVITY": "0.7",
            }
        )

        assert received == {
            "model_path": "models/hey-booth.onnx",
            "threshold": 0.7,
        }

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown WAKE_BACKEND"):
            wake.make_backend(env={"WAKE_BACKEND": "magic"})

    def test_porcupine_requires_access_key(self):
        with pytest.raises(RuntimeError, match="PICOVOICE_ACCESS_KEY"):
            wake.make_backend(
                env={"WAKE_BACKEND": "porcupine", "PICOVOICE_ACCESS_KEY": ""}
            )

    def test_sensitivity_env_is_applied(self):
        b = wake.make_backend(
            env={"WAKE_BACKEND": "stub", "VOICE_WAKE_SENSITIVITY": "0.75"}
        )
        assert b.threshold == 0.75

    def test_bad_sensitivity_falls_back_to_default(self):
        b = wake.make_backend(
            env={"WAKE_BACKEND": "stub", "VOICE_WAKE_SENSITIVITY": "not-a-number"}
        )
        assert b.threshold == wake.DEFAULT_WAKE_THRESHOLD


class TestTriggered:
    def test_detects_on_first_hit(self):
        b = wake.StubBackend(confidence=1.0, threshold=0.5)
        frames = [b"silence"] * 5 + [b"wake"]
        # Stub only fires once -- wake_phrase() on the first call. So
        # triggered() should be True because the first frame hits.
        assert wake.triggered(b, frames) is True

    def test_silent_stream_returns_false(self):
        b = wake.StubBackend(confidence=0.0, threshold=0.5)
        assert wake.triggered(b, [b"silence"] * 10) is False
