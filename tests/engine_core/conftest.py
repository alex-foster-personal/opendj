"""Re-export the webui daemon fixtures so emit-point tests drive real routes.

The emit points live in the legacy routers, so proving them needs the real
app + seeded backend, not a second copy of either. pytest cannot reach a
sibling directory's conftest, and ``pytest_plugins`` is rootdir-only, so the
fixtures are imported here by name -- the ONE supported way to share them.
"""
from __future__ import annotations

from tests.webui.conftest import (  # noqa: F401  (re-exported as fixtures)
    client,
    seed_backend,
)
