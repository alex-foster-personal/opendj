"""Pytest helpers scoped to ``tests/fixtures/``.

Exposes:

* :func:`requires_fixture` - a decorator that skips the wrapped test if
  a named fixture isn't resolvable on the current machine. For OPTIONAL,
  dev-machine-only fixtures (e.g. ``rb-usb-export-big``) where a missing
  external host must never break the suite.
* :func:`resolve_required_fixture` - resolves a REQUIRED acceptance
  fixture, failing closed (not skipping) unless the caller explicitly
  opts out via ``MDT_ALLOW_MISSING_FIXTURES=1``. For fixtures backing
  named requirement/acceptance tests (CAT-*, GUARD-*), where a silent
  skip would drop coverage and still report a non-failing run (AGENTS.md:
  "Never silently skip acceptance because data ... is missing").

The sibling ``big_usb_fixture`` pytest fixture lives in the repo-root
``conftest.py`` so that tests anywhere under ``tests/`` can consume it
(pytest conftests only propagate fixtures downward, and tests outside
``tests/fixtures/`` wouldn't see a fixture defined here).
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, TypeVar

import pytest

from tests.fixtures._resolver import (
    FixtureContractMismatch,
    FixtureNotAvailable,
    fixture_path,
    verify_fixture_contract,
)

__all__ = ["requires_fixture", "resolve_required_fixture"]

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


def _fail_closed_unless_missing_allowed(detail: str) -> NoReturn:
    """Fail unless ``MDT_ALLOW_MISSING_FIXTURES=1`` explicitly opts out.

    Shared by :func:`resolve_required_fixture` and the root ``conftest.py``
    rekordbox fixture (which keeps its own copy with more specific wording,
    since it names the concrete downstream coverage that would be lost).

    ``allow_module_level=True`` because ``resolve_required_fixture`` is called
    from module-level constants (e.g. ``FIXTURE = resolve_required_fixture(...)``
    in ``test_pioneer_reader.py``), not only from inside test/fixture function
    bodies; plain ``pytest.skip()`` raises ``Failed: ... pass
    allow_module_level=True`` in that context instead of skipping.
    """
    if os.environ.get("MDT_ALLOW_MISSING_FIXTURES") == "1":
        pytest.skip(f"{detail} (MDT_ALLOW_MISSING_FIXTURES=1 set)", allow_module_level=True)
    else:
        pytest.fail(
            f"{detail} Set MDT_ALLOW_MISSING_FIXTURES=1 to explicitly skip "
            "on a machine that knowingly lacks the fixture host; unset, "
            "this fails closed rather than silently dropping acceptance "
            "coverage (AGENTS.md: never silently skip acceptance for "
            "missing data)."
        )
    raise AssertionError("unreachable: pytest.skip/pytest.fail always raise")


def resolve_required_fixture(name: str) -> Path:
    """Resolve a REQUIRED acceptance fixture, failing closed when missing.

    Use for fixtures that back a named requirement/acceptance test
    (``@pytest.mark.requirement("CAT-06")`` etc.) rather than an optional
    dev-machine convenience fixture -- see :func:`requires_fixture` for
    that case. A skip here would let CI and a fresh public clone report a
    non-failing run while silently dropping the acceptance evidence the
    marker exists to guarantee.

    Once resolved, the fixture's complete content is verified against
    ``tests/fixtures/<name>.contract.json`` (see
    :func:`tests.fixtures._resolver.verify_fixture_contract`) before the
    path is handed back, so a stale or partially copied external host is
    rejected rather than silently used. Never gated by
    ``MDT_ALLOW_MISSING_FIXTURES`` -- that variable is about an unavailable
    host, not a corrupted or stale one that IS available.
    """
    try:
        root = fixture_path(name)
    except (FixtureNotAvailable, FileNotFoundError) as exc:
        _fail_closed_unless_missing_allowed(f"Fixture {name!r} not available: {exc}")
    try:
        verify_fixture_contract(name, root)
    except FixtureContractMismatch as exc:
        pytest.fail(str(exc))
    return root
