"""Direct contract tests for apps.shared.id_grammar.require_matching (#7428)."""

from __future__ import annotations

import re

import pytest

from apps.shared.id_grammar import require_matching

_BACKUP_ID = re.compile(r"[0-9a-f]{32}")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def test_matching_value_returned_unchanged() -> None:
    value = "abc123def4567890abcdef1234567890"
    assert require_matching(
        value,
        _BACKUP_ID,
        field="backup_id",
        description="32 lowercase hex characters (uuid4().hex)",
    ) is value


def test_default_refuse_raises_value_error_with_exact_message() -> None:
    bad = "not-hex"
    with pytest.raises(ValueError) as exc:
        require_matching(
            bad,
            _BACKUP_ID,
            field="backup_id",
            description="32 lowercase hex characters (uuid4().hex)",
        )
    assert str(exc.value) == (
        f"backup_id must be 32 lowercase hex characters (uuid4().hex), got {bad!r}"
    )


def test_custom_refuse_receives_same_message() -> None:
    bad = "bad-slug-"
    seen: list[str] = []

    class CustomError(Exception):
        pass

    def capture(message: str) -> Exception:
        seen.append(message)
        return CustomError(message)

    with pytest.raises(CustomError):
        require_matching(bad, _SLUG, field="map id", description="a lowercase slug", refuse=capture)
    assert seen == [f"map id must be a lowercase slug, got {bad!r}"]


def test_fullmatch_rejects_trailing_junk_with_unanchored_pattern() -> None:
    exact = "a" * 32
    junk = exact + "x"
    assert require_matching(
        exact,
        _BACKUP_ID,
        field="backup_id",
        description="32 lowercase hex characters (uuid4().hex)",
    ) == exact
    with pytest.raises(ValueError):
        require_matching(
            junk,
            _BACKUP_ID,
            field="backup_id",
            description="32 lowercase hex characters (uuid4().hex)",
        )


def test_backup_id_boundaries() -> None:
    valid = "0123456789abcdef0123456789abcdef"
    assert (
        require_matching(
            valid,
            _BACKUP_ID,
            field="backup_id",
            description="32 lowercase hex characters (uuid4().hex)",
        )
        == valid
    )
    for bad in (
        "0" * 31,
        "0" * 33,
        "0123456789ABCDEF0123456789ABCDEF",
        "../../../etc/passwd",
    ):
        with pytest.raises(ValueError):
            require_matching(
                bad,
                _BACKUP_ID,
                field="backup_id",
                description="32 lowercase hex characters (uuid4().hex)",
            )


def test_slug_boundaries() -> None:
    valid = "ddj-rev7"
    assert require_matching(valid, _SLUG, field="id", description="a lowercase slug") == valid
    for bad in ("UPPER", "trailing-", "sp ace", "../escape"):
        with pytest.raises(ValueError):
            require_matching(bad, _SLUG, field="id", description="a lowercase slug")


def test_does_not_normalize_input() -> None:
    mixed = "AbCdEf0123456789abcdef0123456789"
    with pytest.raises(ValueError):
        require_matching(
            mixed,
            _BACKUP_ID,
            field="backup_id",
            description="32 lowercase hex characters (uuid4().hex)",
        )


def test_does_not_strip_whitespace_before_matching() -> None:
    """Leading or trailing space must fail fullmatch, not be silently stripped."""
    inner = "0123456789abcdef0123456789abcdef"
    for padded in (f" {inner}", f"{inner} ", f" {inner} "):
        with pytest.raises(ValueError):
            require_matching(
                padded,
                _BACKUP_ID,
                field="backup_id",
                description="32 lowercase hex characters (uuid4().hex)",
            )
    assert require_matching(
        inner,
        _BACKUP_ID,
        field="backup_id",
        description="32 lowercase hex characters (uuid4().hex)",
    ) is inner
