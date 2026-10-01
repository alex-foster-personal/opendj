"""Pytest plugin: credit coverage from child Python processes to the test that spawned them.

Loaded only by ``scripts/suite_census.py`` (``-p scripts.pytest_child_coverage``); a normal
pytest run never imports it.

coverage.py's ``[run] patch = subprocess`` starts coverage in every child Python process, but
the parent hands the child its configuration already resolved, as base64 JSON in
``COVERAGE_PROCESS_CONFIG``. Environment-variable expansion in the rc file therefore cannot
label a child per test: it was expanded once, at parent start. A runtest hook rewrites that
config's ``context`` to ``<nodeid>|subprocess`` for each test's setup, call and teardown, so the child's
lines are recorded under the test that launched it.

Fails loud: if coverage hands over a config without a ``context`` key, the format changed
under us and the census must not silently fall back to unlabelled child data.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator

import pytest

PROCESS_CONFIG_ENV = "COVERAGE_PROCESS_CONFIG"
CHILD_CONTEXT_SUFFIX = "|subprocess"


def child_context_config(encoded: str, nodeid: str) -> str:
    """Return ``encoded`` with its coverage ``context`` set to ``nodeid|subprocess``."""
    config = json.loads(base64.b64decode(encoded))
    if "context" not in config:
        raise KeyError(f"{PROCESS_CONFIG_ENV} has no 'context' key; keys: {sorted(config)}")
    config["context"] = f"{nodeid}{CHILD_CONTEXT_SUFFIX}"
    return base64.b64encode(json.dumps(config).encode()).decode()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[None]:
    """Label children for the item's setup, call AND teardown.

    A function-scoped fixture would start after session-, module- and class-scoped fixture setup
    and end before their teardown, so children spawned there stayed unlabelled and were then
    discarded as import-time arcs (Sol P1, #4777). Those broader fixtures set up inside the first
    item that needs them and tear down inside the last one, which is the same item pytest-cov's
    in-process ``--cov-context=test`` credits them to.
    """
    encoded = os.environ.get(PROCESS_CONFIG_ENV)
    if encoded is None:
        yield
        return
    os.environ[PROCESS_CONFIG_ENV] = child_context_config(encoded, item.nodeid)
    try:
        yield
    finally:
        os.environ[PROCESS_CONFIG_ENV] = encoded
