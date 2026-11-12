"""Re-export the health-light library fixture for the stems job truth tests.

The stems job's target set IS the enrich card's measurement (STEM-58), so its
tests run against the same real state.db and real coverage routes the card's
own tests use. pytest cannot reach a sibling directory's conftest, so the
fixture is imported here by name, the same way tests/engine_core/conftest.py
shares the webui fixtures.
"""

from __future__ import annotations

from tests.health_lights.conftest import library  # noqa: F401  (re-exported as a fixture)
from tests.stems.stems_separate_wire_helpers import (  # noqa: F401  (re-exported as fixtures)
    client,
    data_dir,
    engine_library,
    shipped_build_env,
)
