"""Evaluate a GitHub Actions `if:` condition against a PR context, for workflow tests.

Covers the grammar the Trunk queue-draft conditions use: `${{ ... }}`, `startsWith`,
`==`, `!=`, `&&`, `||`, `!` before `(` or a call, parentheses, 'string' literals,
true/false/null, and `github.*`, `needs.*` and `inputs.*` context paths. Comparison follows GitHub's documented
rules (https://docs.github.com/en/actions/reference/workflows-and-actions/expressions):
"GitHub ignores case when comparing strings", and when the operand types differ both are
coerced to a number (null 0, true 1, false 0, a string parsed as a JSON number, '' 0,
otherwise NaN, which equals nothing). An unmodelled token raises ValueError and a context
path the caller did not supply raises KeyError, so an unmodelled condition is a loud test
failure, never a silent guess. tests/quality/test_gha_conditions.py checks the verdicts
against real runs from this repository's Actions history.

-Claude
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

TRUNK_APP_LOGIN = "trunk-io[bot]"  # REST user.login on Trunk's draft PRs, e.g. #4355 (id 85644782)
REPO = "private_owner/music-dj-tools"

Scalar = str | bool | int | float | None


@dataclass(frozen=True)
class PrEvent:
    head_ref: str
    author: str
    head_repo: str = REPO

    def context(self) -> dict[str, Scalar]:
        return {
            "github.head_ref": self.head_ref,
            "github.event.pull_request.user.login": self.author,
            "github.event.pull_request.head.repo.full_name": self.head_repo,
            "github.repository": REPO,
            "github.event_name": "pull_request",
        }


GENUINE_DRAFT = PrEvent("trunk-merge/pr-4227/0b1c2d3e", TRUNK_APP_LOGIN)
SPOOFED_BRANCH = PrEvent("trunk-merge/bypass", "some-human")
SPOOFED_FORK = PrEvent("trunk-merge/bypass", TRUNK_APP_LOGIN, head_repo="attacker/music-dj-tools")
ORDINARY_PR = PrEvent("af--some-feature", "some-human")
ALL_EVENTS = (GENUINE_DRAFT, SPOOFED_BRANCH, SPOOFED_FORK, ORDINARY_PR)

_TOKEN = re.compile(
    r"\s+|(?P<string>'(?:[^']|'')*')|(?P<path>(?:github|needs|inputs)(?:\.[\w-]+)+)|(?P<keyword>true|false|null)\b"
    r"|(?P<call>startsWith\()|(?P<op>&&|\|\||==|!=|!(?=\s*(?:\(|startsWith\())|[(),])"
)
_PYTHON_OP = {"&&": " and ", "||": " or ", "==": " == ", "!=": " != ", "(": "(", ")": ")", ",": ","}
_KEYWORD: dict[str, Scalar] = {"true": True, "false": False, "null": None}


# -----------------------------------------------------------------------------
def condition_holds(condition: str | bool | None, event: PrEvent | Mapping[str, Scalar]) -> bool:
    if condition is None:
        return True
    if isinstance(condition, bool):
        return condition
    text = condition.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    context = event.context() if isinstance(event, PrEvent) else event
    return bool(
        eval(
            _to_python(text, context),
            {"__builtins__": {}, "_V": _Value, "_starts_with": _starts_with},
        )
    )


def _to_python(text: str, context: Mapping[str, Scalar]) -> str:
    """One left-to-right pass, so a literal's contents can never be read as an operator."""
    parts: list[str] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise ValueError(f"unmodelled token at {text[position:]!r} in {text!r}")
        position = match.end()
        kind, token = match.lastgroup, match.group(0)
        if kind is None:
            continue
        if kind == "string":
            parts.append(f"_V({token[1:-1].replace(chr(39) * 2, chr(39))!r})")
        elif kind == "path":
            parts.append(f"_V({context[token]!r})")
        elif kind == "keyword":
            parts.append(f"_V({_KEYWORD[token]!r})")
        elif kind == "call":
            parts.append("_starts_with(")
        elif kind == "op":
            parts.append(" not " if token == "!" else _PYTHON_OP[token])
    return "".join(parts)


@dataclass(frozen=True, eq=False)
class _Value:
    raw: Scalar

    def __eq__(self, other: object) -> bool:
        assert isinstance(other, _Value), other
        # "GitHub ignores case when comparing strings."
        if isinstance(self.raw, str) and isinstance(other.raw, str):
            return self.raw.casefold() == other.raw.casefold()
        if type(self.raw) is type(other.raw):
            return self.raw == other.raw
        # Mismatched types: loose equality by number coercion, and NaN equals nothing.
        return _as_number(self.raw) == _as_number(other.raw)

    def __ne__(self, other: object) -> bool:
        return not self == other

    def __hash__(self) -> int:
        raise TypeError("unhashable: equality here is GitHub's loose, case-insensitive equality")

    def __bool__(self) -> bool:
        return self.raw not in (None, False, 0, "") and not (
            isinstance(self.raw, float) and math.isnan(self.raw)
        )


def _as_number(value: Scalar) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool | int | float):
        return float(value)
    if value == "":
        return 0.0
    try:
        # NaN and Infinity are not legal JSON numbers.
        parsed = json.loads(value, parse_constant=lambda _: math.nan)
    except ValueError:
        return math.nan
    return (
        float(parsed)
        if isinstance(parsed, int | float) and not isinstance(parsed, bool)
        else math.nan
    )


def _starts_with(value: _Value, prefix: _Value) -> bool:
    """GitHub's startsWith ignores case."""
    return str(value.raw).casefold().startswith(str(prefix.raw).casefold())
