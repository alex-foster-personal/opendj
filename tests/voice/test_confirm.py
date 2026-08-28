"""Unit tests for apps.voice.confirm (VOICE-01)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.voice import confirm
from apps.voice import tts as tts_mod

pytestmark = pytest.mark.requirement("VOICE-01")


class TestParseYesNo:
    @pytest.mark.parametrize(
        "text",
        ["yes", "yeah", "yep", "yup", "confirm", "ok", "okay", "do it", "sure", "affirmative"],
    )
    def test_yes_variants(self, text):
        assert confirm.parse_yes_no(text) is True

    @pytest.mark.parametrize(
        "text",
        ["no", "nope", "nah", "cancel", "abort", "stop", "negative"],
    )
    def test_no_variants(self, text):
        assert confirm.parse_yes_no(text) is False

    @pytest.mark.parametrize(
        "text",
        ["maybe", "hmm", "I'm not sure", "", "   ", "what?"],
    )
    def test_ambiguous(self, text):
        assert confirm.parse_yes_no(text) is None

    def test_none_input(self):
        assert confirm.parse_yes_no(None) is None  # type: ignore[arg-type]


class TestConfirmEngine:
    def _engine(self, responses: list[str], tmp_path: Path):
        resps = list(responses)

        def fake_listen(timeout_s: float) -> str:
            return resps.pop(0) if resps else ""

        return confirm.ConfirmEngine(
            tts_engine=tts_mod.RecordingTts(),
            listen_fn=fake_listen,
            log_path=tmp_path / "confirmations.jsonl",
            timeout_s=0.1,
        )

    def test_yes_accepted(self, tmp_path):
        engine = self._engine(["yes please"], tmp_path)
        result = engine.confirm("did you say rate five stars?")
        assert result.accepted is True
        assert result.ambiguous is False
        assert result.timed_out is False

    def test_no_rejected(self, tmp_path):
        engine = self._engine(["no cancel"], tmp_path)
        result = engine.confirm("confirm?")
        assert result.accepted is False

    def test_timeout_returns_false(self, tmp_path):
        engine = self._engine([""], tmp_path)
        result = engine.confirm("confirm?")
        assert result.accepted is False
        assert result.timed_out is True

    def test_ambiguous_returns_false(self, tmp_path):
        engine = self._engine(["maybe later"], tmp_path)
        result = engine.confirm("confirm?")
        assert result.accepted is False
        assert result.ambiguous is True

    def test_log_entry_appended(self, tmp_path):
        log = tmp_path / "confirmations.jsonl"
        engine = self._engine(["yes"], tmp_path)
        engine.log_path = log
        engine.confirm("prompt X")
        rows = [json.loads(l) for l in log.read_text(encoding="utf-8").strip().splitlines()]
        assert len(rows) == 1
        assert rows[0]["prompt"] == "prompt X"
        assert rows[0]["decision"] is True
        assert rows[0]["transcript"] == "yes"

    def test_tts_spoke_prompt(self, tmp_path):
        engine = self._engine(["yes"], tmp_path)
        engine.confirm("did you say rate 5 stars?")
        assert engine.tts_engine.spoken == ["did you say rate 5 stars?"]


class TestBindConfirm:
    def test_bind_returns_callable(self, tmp_path):
        calls: list[str] = []

        def fake_listen(timeout_s: float) -> str:
            return "yes"

        engine = confirm.ConfirmEngine(
            tts_engine=tts_mod.RecordingTts(),
            listen_fn=fake_listen,
            log_path=tmp_path / "log.jsonl",
            timeout_s=0.1,
        )
        fn = confirm.bind_confirm(engine)
        assert fn(None, "prompt?") is True


class TestAppendConfirmationLog:
    def test_writes_jsonl_row(self, tmp_path):
        log = tmp_path / "a" / "b" / "log.jsonl"
        confirm.append_confirmation_log(
            log, prompt="p", transcript="t", decision=True, meta={"extra": 1}
        )
        row = json.loads(log.read_text(encoding="utf-8").strip())
        assert row["prompt"] == "p"
        assert row["decision"] is True
        assert row["extra"] == 1
