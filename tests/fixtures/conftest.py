"""Pytest helpers scoped to ``tests/fixtures/``.

Exposes:

* :func:`requires_fixture` — a decorator that skips the wrapped test if
  a named fixture isn't resolvable on the current machine.

The sibling ``big_usb_fixture`` pytest fixture lives in the repo-root
``conftest.py`` so that tests anywhere under ``tests/`` can consume it
(pytest conftests only propagate fixtures downward, and tests outside
``tests/fixtures/`` wouldn't see a fixture defined here).
"""
from __future__ import annotations

from typing import Callable, TypeVar

import pytest

from tests.fixtures._resolver import (
    FixtureNotAvailable,
    fixture_path,
)

__all__ = ["requires_fixture"]

F = TypeVar("F", bound=Callable[..., object])


def requires_fixture(name: str) -> Callable[[F], F]:
    """Skip the decorated test when ``name`` cannot be resolved.

    Use when a single test needs a specific fixture but you don't want
    the whole module to pay the resolver-import cost up front.

    Example
    -------
    >>> @requires_fixture("rb-usb-export-big")
    ... def test_something(): ...

    Implementation note: we resolve eagerly at import time (not at test
    call time) so that pytest's collection phase picks up the skip
    reason and the test is visibly greyed out in the report. This
    matches the stylistic choice in ``tests/sync/usb/test_pioneer_reader.py``
    where the module-level ``FIXTURE`` check raises at collection.
    """
    def decorator(fn: F) -> F:
        try:
            fixture_path(name)
        except (FixtureNotAvailable, FileNotFoundError) as exc:
            return pytest.mark.skip(  # type: ignore[return-value]
                reason=f"Fixture {name!r} not available: {exc}"
            )(fn)
        return fn

    return decorator
