"""Classify one harvested prompt: regex pre-filter, then LLM structured output.

The closed-verb regex (PR #3642 lineage) decides obvious rows without a model
call. Verbs outside that vocabulary, including Improve, Correct, Protect, and
Introduce, go to an OpenRouter chat completion with a pydantic schema
``{behaviour_bearing: bool, reason: str}``. When the model cannot be reached the
caller gets UNAVAILABLE, never a guessed bool.

Regex reference (pre-filter only):

  - Imperative sentence: please add/fix/implement/make/ensure/ship/merge/...,
    including declarative asks (we need to / we should / we must / can we /
    I'd like you to) and a leading filler word (ok, oh, then, so, now, first)
  - Question about behavior: should/must/can, could, would, will we/does/...?,
    a first-person can I <action>? / do I need to ...?, a modal question about
    something else (could it be off ...?), or ? with product verbs
  - Excludes compaction summaries, supervisor and peer-session relays, and
    automation templates (voice summary, Codex approval review), by first line

Imperatives are matched at the start of every SENTENCE, or clause after a
spoken " - ", not only of every line: prompts are often one run-on line with
the ask in its second or third sentence (codex review of PR #3642 at
747b70e12: "OLTF Also had ... We need to get much better at tracking analyses
..." was never audited).

Declarative ask MARKERS (we need / we should / we must / we have to / I need,
want, 'd like or would like you or us to / can, could, would, will you /
please / let's), and an outcome request (I or we want, I'd like + the thing
wanted, not "to <verb>"), also count anywhere in
a clause, after introductory context: "This makes me realize we need another
UI agent ..." (codex review at 9731112b8). Bare imperative verbs stay anchored
to the start of a sentence, so a product verb mid-sentence is not an ask.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_ENV = "OPENROUTER_API_KEY"
MODEL_ENV = "PROMPT_ASKS_MODEL"

_LIST_MARKER_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
# "can / could / would / will we ...?" alike, as _DECLARATIVE_RE takes all four
# with "you": "could we have it come off the LHS of Air's screen?" is an ask
# (codex review of PR #3642 at e4343383c).
_BEHAVIOUR_QUESTION_RE = re.compile(
    r"(?i)\b(should|must|(?:can|could|would|will)\s+we|does|why does|what happens when"
    r"|is it possible)\b.+\?"
)
# A first-person question about what the product lets you do, or makes you do:
# "can I see it in UI?", "do I need to refresh the page?" (codex review of PR
# #3642 at bb05f5b3a). The boundary: can / could / may I + an action asks about a
# capability, but + a speech act or a state ("Can I ask what that is?", "Could
# I be wrong?") is conversation; do / will / would I asks only as "need to" or
# "have to", so "Do I look ok?" is chat.
_FIRST_PERSON_QUESTION_RE = re.compile(
    r"(?i)\b(?:(?:can|could|may)\s+i\s+(?!(?:(?:just|also)\s+)?(?:ask|say|tell|mention|be)\b)\w"
    r"|(?:do|will|would)\s+i\s+(?:need|have)\s+to\b).*\?"
)
# One vocabulary for both the imperative first word and a product question.
# It is deliberately broad: a behaviour ask that falls outside it is silently
# never audited, while an over-inclusion costs one NOTE disposition.
# flip / generate / leave / put / quit / search: each opens a real ledger ask
# that was missed ("How is confidence calculated? put a note at top of UI",
# "Plz quit DJ software ... Leave oDj open atm."; codex review of PR #3642 at
# 5c6a52c7a). Measured on the committed ledger and left OUT: speech acts (ask,
# tell, give, explain, reply, remind, report), whose question form is chat
# ("Can I ask what that is?"), and words a question or narration opens with as
# often as an ask (try, see, read, note, return, research: "What are you quick
# thoughts (without research) on that opinion?" would count).
# fmt: off
_PRODUCT_VERBS = frozenset({
    "add", "allow", "audit", "block", "build", "bump", "change", "check", "clean",
    "close", "commit", "configure", "continue", "create", "debug", "delete",
    "deploy", "design", "diagnose", "disable", "display", "document", "drop",
    "enable", "ensure", "export", "expose", "filter", "find", "finish", "fix",
    "get", "handle", "hide", "implement", "import", "install", "investigate",
    "keep", "land", "list", "load", "log", "make", "merge", "migrate", "move",
    "open", "optimize", "parse", "persist", "play", "port", "post", "prevent",
    "publish", "push", "rebase", "record", "reduce", "refactor", "refuse",
    "release", "remove", "rename", "render", "replace", "resolve", "restart",
    "restore", "retry", "revert", "review", "rewrite", "run", "save", "send",
    "set", "ship", "show", "simplify", "sort", "split", "start", "stop",
    "support", "sync", "test", "track", "undo", "update", "upgrade", "use",
    "validate", "verify", "wire", "write",
    "flip", "generate", "leave", "put", "quit", "search",
    "rerun",  # codex review of PR #3642 at 9e8a7ae95
})
# fmt: on
_VERB_ALTERNATION = "|".join(sorted(_PRODUCT_VERBS, key=len, reverse=True))
_PRODUCT_VERB_RE = re.compile(rf"(?i)\b(?:{_VERB_ALTERNATION})\b")
# A modal question about what something else could be or do: "could it be off
# MBP right edge easily?", "can Docker be on D?" (codex review of PR #3642 at
# 83c49f69d). The subject is one to three words and never I / we / you, which
# the first-person rule and the "can you" marker already take; the verb is
# "be" + a complement or a listed verb, so "Would wrapping C++ ... be a massive
# pain?" (a six-word subject) is left for a person to read.
_ADVERBS = r"also|still|just|easily|really|only|never|not|rarely|ever"
# "be" must be followed by what the thing would be MADE, not by an identity:
# "What could it be?" and "Would that be the new one?" ask what something is.
_BE_COMPLEMENT = r"(?!(?:the|a|an|that|this|it|my|your|his|her|their|our)\b)\w"
_MODAL_QUESTION_RE = re.compile(
    rf"(?i)\b(?:can|could|would|will)\s+(?!(?:i|we|you|{_ADVERBS})\b)(?:[\w'./-]+\s+){{1,3}}?"
    rf"(?:(?:{_ADVERBS})\s+)?(?:be\s+{_BE_COMPLEMENT}|(?:{_VERB_ALTERNATION})\b)"
)
# Discourse fillers a spoken ask often opens with: "OK - run the servers",
# "Then land the fix", "Oh - first update the skill".
# Connectives and conditionals introduce the imperative after them and change
# nothing about it: "on main yet? else make it so", "If not - fix them", "or run
# directly" (codex review of PR #3642 at 5544687c2). A lead-in alone is never an
# ask: a product verb must still follow ("else it's fine" stays out).
_LEAD_IN = (
    r"(?:(?:ok(?:ay)?|oh|so|and|also|now|then|first|next|but|else|otherwise|or|instead"
    r"|anyway|if\s+(?:not|so|yes)|pls|plz)\b[\s,.\-]+)*"
)
# A generic directive that names no product verb: an approval or a
# continuation ("OK do it", "Do it - I'll fiddle around", "yes - do until done",
# "OK - press on"; codex review of PR #3642 at 9e8a7ae95). Never a question: the
# rest of its sentence may not end in "?", so "do it?" stays out, and "did you do
# it" or "how do I do it" never open with it. "look into it" is one too, and a
# hyphenated "re-" re-applies a listed verb: "re-enable deskflow on PC".
_DIRECTIVE = (
    r"(?:do\s*(?:it|that|this|until)|go\s+ahead|go\s+for\s+it|carry\s+on|press\s+on"
    r"|proceed|look\s+into)\b(?![^.!?]*\?)"
)
# A delegated ask: "have <agent> <verb>", where <verb> is a listed verb or
# a generic directive ("have SA run ram cleanup skill and find 25-50gb we can
# free up locally."; codex review of PR #3642 at 990c327d2). "get" and "make"
# are listed verbs already. A subject pronoun is a question or a report, not an
# agent ("have you run it?", "have they finished"), and a "?" ends it too.
_DELEGATED = (
    r"have\s+(?!(?:i|we|you|they|he|she)\b)(?:[\w'./-]+\s+){1,3}?(?:to\s+)?"
    rf"(?:(?:re-)?(?:{_VERB_ALTERNATION})\b|{_DIRECTIVE})(?![^.!?]*\?)"
)
_IMPERATIVE_RE = re.compile(
    rf"(?i)^{_LEAD_IN}"
    r"(?:please\s+|(?:can|could|would|will)\s+you\s+(?:please\s+)?|"
    r"(?:i|we)\s+(?:want|need)\s+(?:you\s+)?to\s+|let['\u2019]?s\s+|"
    r"(?:we|you)\s+(?:should|must|need\s+to|have\s+to)\s+(?:also\s+)?|"
    r"(?:can|could)\s+we\s+|i['\u2019]?d\s+like\s+(?:you\s+)?to\s+)?"
    r"(?:(?:do\s+not|don['\u2019]?t|never|not)\s+)?"
    rf"(?:(?:re-)?(?:{_VERB_ALTERNATION})\b|{_DIRECTIVE}|{_DELEGATED})"
)
# An ask marker anywhere in a clause; `\b` on both sides, so "pleased" or a
# "we" that does not directly precede "need" does not match. "we should be" /
# "we must be" states an expectation ("we should be fine now"), not an ask.
_DECLARATIVE_RE = re.compile(
    r"(?i)\b(?:we\s+(?:really\s+|also\s+|still\s+)?(?:need|(?:should|must)(?!\s+be\b)|have\s+to)"
    r"|i\s+(?:really\s+)?(?:need|want)\s+(?:you|us)\s+to"
    r"|i(?:['\u2019]d|\s+would)\s+like\s+(?:you|us)\s+to"
    r"|(?:can|could|would|will)\s+you|please|let['\u2019]?s)\b"
)
# An OUTCOME request: "I / we want" or "I'd / I would like" + the thing wanted,
# "I want refactor done by 5am" (codex review of PR #3642 at 1444aa80b). The
# boundary is what follows: "to <verb>" is the speaker's own intent or curiosity
# ("I want to know why", counted only when that verb is a product verb, by
# _IMPERATIVE_RE), and "you / us" is the ask form above; "wanted" is a report.
_OUTCOME_RE = re.compile(
    r"(?i)\b(?:(?:i|we)\s+(?:really\s+|just\s+|also\s+|still\s+)?want"
    r"|i(?:['\u2019]d|\s+would)\s+(?:really\s+)?like)\s+(?!(?:to|you|us)\b)\w"
)
# A sentence ends at . ! ? ; or : followed by whitespace, or at a spoken dash,
# " - " or " -- " with space on both sides ("Done - continue.", "it IS running -
# open config for it plz"; 57 committed rows, Tue 29 Sep 2026, codex review of PR
# #3642 at 9e8a7ae95). An unspaced hyphen ("re-enable", "read-only") is a word.
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?;:])\s+|\s+-{1,2}\s+")
# First lines that only harness or automation writes. Each template below was
# measured on the committed ledger (first 60 chars of the first line, grouped;
# every prefix recurring 10+ times read, Tue 29 Sep 2026; codex review of PR
# #3642 at cdc2e9378). Codex's "# Files mentioned by the user:" wrapper (17
# rows) is NOT here: the human's own request follows it.
_EXCLUDE_PREFIXES = (
    "This session is being continued",
    "the maintainer is asleep",
    "the maintainer has left",
    # 4 rows, Wed 9 Sep 2026: the same supervisor relay sent to four sessions
    # (codex review of PR #3642 at 0444aff14).
    "the maintainer is out for the evening",
    "NOTHING PENDING",
    # 212 rows: the end-of-turn voice-summary generator's prompt.
    "You are the maintainer's friendly voice assistant giving him a quick spoken update",
    # 110 rows: a peer session's relay, which says it is "not typed by your user".
    "Another Claude session sent a message:",
    # 32 + 5 rows: Codex's auto-approval reviewer, quoting a transcript as evidence.
    "The following is the Codex agent history added since your last approval assessment.",
    "The following is the Codex agent history whose request action you are assessing.",
)
# 10 rows, byte-identical: the harness's resume nudge after a usage-limit reset.
# Matched on the WHOLE text, so a human adding anything to it is still read.
_EXCLUDE_EXACT = frozenset(
    {
        "I hit my usage limit while you were working, but it has reset now. Please continue from where you left off.",
    }
)


# Positive NOT-ask shapes (the three states: see classify). Each is a whole
# clause, so a clause that is neither an ask, a question nor one of these leaves
# the row UNCLASSIFIED, listed by the audit rather than silently dropped.
_ACK_RE = re.compile(
    r"(?i)^(?:(?:ok(?:ay)?|thanks|thank\s+you|ty|great|nice|cool|done|yes|yep|yeah|no"
    r"|nope|sure|perfect|good|fine|got\s+it|hi|hello|hey|awesome|right)[\s!.,:)]*)+$"
)
# Issue #11336 acceptance chatter (whole prompt, one line).
_CHATTER_RE = re.compile(r"(?i)^(?:thanks|thank\s+you|looks\s+good|standing\s+by)(?:[.!,\s]*)$")
# A question is not evidence of a non-ask: "could it be off MBP right edge
# easily?" asks for a behavior (codex review of PR #3642 at 83c49f69d). The one
# question taken as positive NOT-ask evidence is a wh-question about state or
# facts ("which branch are you on?", "how are the agents doing?", "eta?") that
# names no product noun and, having reached here, no listed verb. "what about X?"
# proposes X, so it is not one; a yes/no question ("merged into main yet?") is
# left UNCLASSIFIED for a person to read.
_PRODUCT_NOUNS = (
    r"ui|deck|decks|waveforms?|playlists?|tracks?|library|stems?|buttons?|pages?|screens?"
    r"|app|features?|cues?|beatgrids?|mixer|bpm|tempo|audio|sidebar|panels?|menus?|settings?"
    r"|requirements?"
)
_INFO_QUESTION_RE = re.compile(
    rf"(?i)^{_LEAD_IN}(?:what|which|where|when|who|how|eta)\b(?!['\u2019]?s?\s+about\b)"
    rf"(?![^?]*\b(?:{_PRODUCT_NOUNS})\b)[^?]*\?$"
)
# A bare harness command ("/compact"); "/goal register my ..." carries an ask.
_SLASH_COMMAND_RE = re.compile(r"^/[\w-]+$")
ASK = "ask"
NOT_ASK = "not-ask"
UNCLASSIFIED = "unclassified"


def _eval_line(text: str) -> str:
    stripped = text.strip()
    for line in stripped.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate[:500]
    return stripped[:500]


def _is_question(clause: str) -> bool:
    return clause.rstrip(")\"'").endswith("?")


def _line_is_ask(line: str) -> bool:
    if _BEHAVIOUR_QUESTION_RE.search(line) or _FIRST_PERSON_QUESTION_RE.search(line):
        return True
    if line.endswith("?") and _PRODUCT_VERB_RE.search(line):
        return True
    if _DECLARATIVE_RE.search(line) or _OUTCOME_RE.search(line):
        return True
    sentences = [_LIST_MARKER_RE.sub("", part) for part in _SENTENCE_BREAK_RE.split(line)]
    if any(_is_question(s) and _MODAL_QUESTION_RE.search(s) for s in sentences):
        return True
    return any(_IMPERATIVE_RE.match(sentence) for sentence in sentences)


def classify(text: str) -> str:
    """Regex-only state: ``ASK``, ``NOT_ASK`` or ``UNCLASSIFIED`` (needs LLM).

    ASK: any line matches the closed verb vocabulary. NOT_ASK on a positive
    match (boilerplate, slash command, ack, info question, or issue #11336
    chatter). Everything else is UNCLASSIFIED and needs the model.
    """
    stripped = text.strip()
    if stripped and _CHATTER_RE.match(stripped):
        return NOT_ASK
    first = _eval_line(text)
    if not first or text.strip() in _EXCLUDE_EXACT or first.startswith(_EXCLUDE_PREFIXES):
        return NOT_ASK
    lines = [_LIST_MARKER_RE.sub("", line.strip()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if any(_line_is_ask(line) for line in lines):
        return ASK
    if len(lines) == 1 and _SLASH_COMMAND_RE.match(lines[0]):
        return NOT_ASK
    clauses = [c.strip() for line in lines for c in _SENTENCE_BREAK_RE.split(line)]
    if all(_INFO_QUESTION_RE.match(c) or _ACK_RE.match(c) for c in clauses if c):
        return NOT_ASK
    return UNCLASSIFIED


class PromptAskClassification(BaseModel):
    """Structured LLM verdict for one user prompt."""

    model_config = ConfigDict(extra="forbid")

    behaviour_bearing: bool
    reason: str


class PromptAskModelUnavailable(RuntimeError):
    """The classifier could not reach OpenRouter."""


LlmClassifier = Callable[[str], PromptAskClassification]


@dataclass(frozen=True)
class PromptAskResult:
    """One classification outcome. ``classification`` is absent when UNAVAILABLE."""

    classification: PromptAskClassification | None
    unavailable: str | None = None
    source: Literal["regex", "llm"] | None = None

    def model_dump_public(self) -> dict[str, Any]:
        if self.unavailable is not None:
            return {"unavailable": self.unavailable}
        assert self.classification is not None
        return {
            "behaviour_bearing": self.classification.behaviour_bearing,
            "reason": self.classification.reason,
            "source": self.source,
        }


_LLM_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "prompt_ask_classification",
        "strict": True,
        "schema": PromptAskClassification.model_json_schema(),
    },
}

_LLM_SYSTEM = (
    "You classify one message the human sent to a coding agent working on a DJ software repo. "
    "behaviour_bearing is true when the message asks for a product, code, test, configuration, or "
    "operations change. It is false for thanks, status, small talk, or questions that only seek "
    "facts without requesting work. Always give a short reason."
)


def _regex_verdict(text: str, state: str) -> PromptAskClassification:
    if state == ASK:
        return PromptAskClassification(
            behaviour_bearing=True,
            reason="regex matched a closed-vocabulary behaviour ask",
        )
    return PromptAskClassification(
        behaviour_bearing=False,
        reason="regex matched non-ask chatter or harness boilerplate",
    )


def _openrouter_classify(text: str) -> PromptAskClassification:
    api_key = os.environ.get(KEY_ENV, "").strip()
    model = os.environ.get(MODEL_ENV, "").strip()
    if not api_key:
        raise PromptAskModelUnavailable(f"{KEY_ENV} is not set")
    if not model:
        raise PromptAskModelUnavailable(f"{MODEL_ENV} is not set")
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": text},
            ],
            "response_format": _LLM_RESPONSE_SCHEMA,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        OPENROUTER_CHAT_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise PromptAskModelUnavailable(f"OpenRouter request failed: {exc}") from exc
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise PromptAskModelUnavailable("OpenRouter response missing message content") from exc
    if not isinstance(content, str) or not content.strip():
        raise PromptAskModelUnavailable("OpenRouter returned empty content")
    try:
        parsed = json.loads(content)
        return PromptAskClassification.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise PromptAskModelUnavailable(f"OpenRouter content failed validation: {exc}") from exc


def classify_prompt(text: str, *, llm: LlmClassifier | None = None) -> PromptAskResult:
    """Classify ``text`` with regex first, then ``llm`` when the regex is inconclusive."""
    state = classify(text)
    if state in (ASK, NOT_ASK):
        verdict = _regex_verdict(text, state)
        return PromptAskResult(classification=verdict, source="regex")
    llm_fn = llm if llm is not None else _openrouter_classify
    try:
        verdict = llm_fn(text)
    except PromptAskModelUnavailable as exc:
        return PromptAskResult(classification=None, unavailable=str(exc))
    return PromptAskResult(classification=verdict, source="llm")


def is_behaviour_bearing(text: str) -> bool:
    """True when ``classify_prompt`` reports a behaviour-bearing ask."""
    result = classify_prompt(text)
    if result.classification is None:
        return False
    return result.classification.behaviour_bearing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify one harvested user prompt.")
    parser.add_argument("prompt", help="the prompt text to classify")
    args = parser.parse_args(argv)
    outcome = classify_prompt(args.prompt)
    payload = outcome.model_dump_public()
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 3 if outcome.unavailable is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
