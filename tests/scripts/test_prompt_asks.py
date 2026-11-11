"""REQHYG-03 LLM prompt classifier (issue #11336).

[if] acceptance rows for regex, LLM, and UNAVAILABLE [then] tests lock them, [else stop].
"""

from __future__ import annotations

import io
import json

import pytest

from scripts import prompt_asks as asks

pytestmark = pytest.mark.requirement("REQHYG-03")


def _stub_llm_factory() -> tuple[list[str], asks.LlmClassifier]:
    calls: list[str] = []

    def _llm(text: str) -> asks.PromptAskClassification:
        calls.append(text)
        lowered = text.lower()
        if any(word in lowered for word in ("improve", "correct", "protect", "introduce")):
            return asks.PromptAskClassification(
                behaviour_bearing=True,
                reason="stub: behaviour change verb",
            )
        return asks.PromptAskClassification(
            behaviour_bearing=False,
            reason="stub: not behaviour-bearing",
        )

    return calls, _llm


@pytest.mark.parametrize(
    "prompt",
    [
        "Improve the waveform renderer so peaks stay visible while scrolling.",
        "Correct the BAR fold toast copy before we ship.",
        "Protect the ledger from regressing rows on re-harvest.",
        "Introduce a CLI flag for dry-run stem exports.",
    ],
)
def test_llm_verbs_outside_vocabulary_are_behaviour_bearing(prompt: str) -> None:
    """[if] Improve/Correct/Protect/Introduce behaviour wording [then] true via LLM, [else stop]."""
    calls, llm = _stub_llm_factory()
    assert asks.classify(prompt) == asks.UNCLASSIFIED
    result = asks.classify_prompt(prompt, llm=llm)
    assert result.source == "llm"
    assert result.classification is not None
    assert result.classification.behaviour_bearing is True
    assert calls == [prompt]


@pytest.mark.parametrize(
    "prompt",
    ["thanks", "looks good", "standing by"],
)
def test_chatter_is_not_behaviour_bearing_without_llm(prompt: str) -> None:
    """[if] thanks/looks good/standing by chatter [then] false without LLM, [else stop]."""
    calls, llm = _stub_llm_factory()

    def _fail_if_called(text: str) -> asks.PromptAskClassification:
        calls.append(text)
        raise AssertionError("LLM must not run for chatter")

    result = asks.classify_prompt(prompt, llm=_fail_if_called)
    assert result.source == "regex"
    assert result.classification is not None
    assert result.classification.behaviour_bearing is False
    assert not calls


def test_unreachable_model_reports_unavailable() -> None:
    """[if] the model cannot be reached [then] UNAVAILABLE with no bool verdict, [else stop]."""

    def _broken(_text: str) -> asks.PromptAskClassification:
        raise asks.PromptAskModelUnavailable("network down")

    result = asks.classify_prompt("Improve logging around sync retries.", llm=_broken)
    assert result.classification is None
    assert result.unavailable is not None
    assert "network down" in result.unavailable


def test_regex_known_verb_skips_llm() -> None:
    """[if] regex matches a known verb form [then] LLM is not required, [else stop]."""
    prompt = "Please fix the regression in scripts/provenance_cli.py"
    calls: list[str] = []

    def _fail_if_called(text: str) -> asks.PromptAskClassification:
        calls.append(text)
        raise AssertionError("LLM must not run when regex already matched")

    result = asks.classify_prompt(prompt, llm=_fail_if_called)
    assert asks.classify(prompt) == asks.ASK
    assert result.source == "regex"
    assert result.classification is not None
    assert result.classification.behaviour_bearing is True
    assert not calls


def test_module_cli_emits_pydantic_shape_with_stubbed_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] python -m scripts.prompt_asks runs [then] stdout is the pydantic JSON shape, [else stop]."""
    prompt = "Introduce structured logging on the harvest path."

    def _llm(text: str) -> asks.PromptAskClassification:
        return asks.PromptAskClassification(behaviour_bearing=True, reason="cli stub")

    monkeypatch.setattr(asks, "_openrouter_classify", _llm)
    buf = io.StringIO()
    monkeypatch.setattr(asks.sys, "stdout", buf)
    assert asks.main([prompt]) == 0
    payload = json.loads(buf.getvalue())
    assert payload["behaviour_bearing"] is True
    assert payload["reason"] == "cli stub"
    asks.PromptAskClassification.model_validate({k: payload[k] for k in ("behaviour_bearing", "reason")})
