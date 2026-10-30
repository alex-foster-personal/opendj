"""Direct contract tests for apps.shared.last_match.require_last_match (#7430)."""

from __future__ import annotations

import re

import pytest

from apps.shared.last_match import require_last_match

_TOKEN = re.compile(r"token=(\w+)", re.MULTILINE)


def test_multiple_matches_return_last_capture() -> None:
    text = "token=first\ntoken=second\ntoken=last\n"
    assert require_last_match(text, _TOKEN, missing_message="no token") == "last"


def test_no_match_raises_value_error_with_exact_message() -> None:
    with pytest.raises(ValueError) as exc:
        require_last_match("no tokens here", _TOKEN, missing_message="no token line")
    assert str(exc.value) == "no token line"


def test_custom_refuse_receives_same_message() -> None:
    seen: list[str] = []

    class CustomError(Exception):
        pass

    def capture(message: str) -> Exception:
        seen.append(message)
        return CustomError(message)

    with pytest.raises(CustomError):
        require_last_match("", _TOKEN, missing_message="missing", refuse=capture)
    assert seen == ["missing"]
