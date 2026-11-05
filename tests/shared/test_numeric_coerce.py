"""Regression tests for apps.shared.numeric_coerce.coerce_optional."""

from __future__ import annotations

import math

import pytest

from apps.shared.numeric_coerce import coerce_optional


def test_none_and_empty_return_none_for_int() -> None:
    assert coerce_optional(None, int) is None
    assert coerce_optional("", int) is None


def test_none_and_empty_return_none_for_float() -> None:
    assert coerce_optional(None, float) is None
    assert coerce_optional("", float) is None


def test_valid_int_string() -> None:
    assert coerce_optional("42", int) == 42


def test_valid_float_string() -> None:
    assert coerce_optional("128.5", float) == 128.5


def test_decimal_string_with_int_converter_returns_none() -> None:
    assert coerce_optional("1.5", int) is None


def test_malformed_int_returns_none() -> None:
    assert coerce_optional("not-a-number", int) is None


def test_type_incompatible_input_returns_none() -> None:
    assert coerce_optional(object(), int) is None


def test_float_nan_preserved() -> None:
    result = coerce_optional("nan", float)
    assert result is not None
    assert math.isnan(result)


def test_float_inf_preserved() -> None:
    result = coerce_optional("inf", float)
    assert result is not None
    assert result == float("inf")


def test_unrelated_exception_propagates() -> None:
    def boom(_: object) -> int:
        raise RuntimeError("unexpected")

    with pytest.raises(RuntimeError, match="unexpected"):
        coerce_optional("1", boom)
