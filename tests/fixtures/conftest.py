"""Pytest helpers for external-host fixtures.

Exposes:

* :func:`requires_fixture` — a decorator that skips the wrapped test if
  a named fixture isn't resolvable on the current machine (e.g. LaCie
  is unmounted).
* :func:`big_usb_fixture` — a pytest fixture for the 1586-track
  Rekordbox USB export hosted at ``rb-usb-export-big`` on the
  external host. Skips cleanly if the host is missing.

Keeping these in ``tests/fixtures/conftest.py`` (rather than the repo
root ``conftest.py``) means they're auto-discovered for any test file
under ``tests/`` without bloating the root conftest.
"""
from __future__ import annotations

from typing import Callable, TypeVar

import pytest

from tests.fixtures._resolver import (
    FixtureNotAvailable,
    fixture_path,
)

__all__ = ["requires_fixture", "big_usb_fixture"]

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


@pytest.fixture
def big_usb_fixture():
    """Yield the Path to the 1586-track LaCie-hosted USB export.

    Skips the test (rather than erroring) if LaCie isn't mounted or the
    subpath is missing — cloning the repo on a fresh machine must not
    break the suite.
    """
    try:
        return fixture_path("rb-usb-export-big")
    except (FixtureNotAvailable, FileNotFoundError) as exc:
        pytest.skip(f"rb-usb-export-big not available: {exc}")
