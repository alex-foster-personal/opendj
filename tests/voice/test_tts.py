"""Unit tests for apps.voice.tts (VOICE-01)."""
from __future__ import annotations


import pytest

from apps.voice import tts


pytestmark = pytest.mark.requirement("VOICE-01")


class FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


class TestBuildSayCmd:
    def test_default_is_plain_say(self):
        cmd = tts.build_say_cmd("hello")
        assert cmd == ("say", "hello")

    def test_with_voice(self):
        cmd = tts.build_say_cmd("hi", voice="the maintainer")
        assert cmd == ("say", "-v", "the maintainer", "hi")

    def test_with_rate(self):
        cmd = tts.build_say_cmd("hi", voice="the maintainer", rate_wpm=175)
        assert cmd == ("say", "-v", "the maintainer", "-r", "175", "hi")


class TestSayTts:
    def test_speak_uses_runner(self):
        captured: list[list[str]] = []

        def runner(argv):
            captured.append(list(argv))
            return FakeProc(0)

        engine = tts.SayTts(voice="the maintainer", runner=runner)
        result = engine.speak("rated 5 stars")
        assert captured == [["say", "-v", "the maintainer", "rated 5 stars"]]
        assert result.text == "rated 5 stars"
        assert result.backend == "say"
        assert result.returncode == 0

    def test_env_voice_default(self, monkeypatch):
        monkeypatch.setenv("VOICE_SAY_VOICE", "Samantha")
        engine = tts.SayTts(runner=lambda argv: FakeProc(0))
        result = engine.speak("hi")
        assert "Samantha" in result.cmd


class TestMakeTts:
    def test_default_say(self):
        engine = tts.make_tts(env={})
        assert isinstance(engine, tts.SayTts)

    def test_recording_factory(self):
        engine = tts.make_tts(env={"TTS_BACKEND": "recording"})
        engine.speak("hello")
        assert engine.spoken == ["hello"]

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            tts.make_tts(env={"TTS_BACKEND": "telepathy"})


class TestRecordingTts:
    def test_records_all_calls(self):
        engine = tts.RecordingTts()
        engine.speak("one")
        engine.speak("two")
        assert engine.spoken == ["one", "two"]
