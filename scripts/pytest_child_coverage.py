"""Pytest plugin: credit coverage from child Python processes to the test that spawned them.

Loaded only by ``scripts/suite_census.py`` (``-p scripts.pytest_child_coverage``); a normal
pytest run never imports it.

coverage.py's ``[run] patch = subprocess`` starts coverage in every child Python process, but
the parent hands the child its configuration already resolved, as base64 JSON in
``COVERAGE_PROCESS_CONFIG``. Environment-variable expansion in the rc file therefore cannot
label a child per test: it was expanded once, at parent start. This fixture rewrites that
config's ``context`` to ``<nodeid>|subprocess`` for the duration of each test, so the child's
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


@pytest.fixture(autouse=True)
def _label_child_coverage(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    encoded = os.environ.get(PROCESS_CONFIG_ENV)
    if encoded is not None:
        monkeypatch.setenv(PROCESS_CONFIG_ENV, child_context_config(encoded, request.node.nodeid))
    yield
