"""One scorer per lane, each exposing the same three names.

A lane scorer module must define `SCORER_VERSION`, `score_bundle(bundle, arms)`
returning the report payload, and `render_table(report)` returning the markdown
the round log carries. The CLI resolves the module through `get_scorer` and
never learns which lane it is driving, which is what lets a fifth lane arrive
without touching the harness.

The version is the contract. Two rounds are comparable only if the ruler did
not move between them, so a change to what any metric MEANS bumps the lane's
SCORER_VERSION and the number is stamped into every artifact and every round
block.
"""

from __future__ import annotations

import importlib
from types import ModuleType

_REQUIRED = ("SCORER_VERSION", "score_bundle", "render_table")


def get_scorer(module_path: str) -> ModuleType:
    """Import a lane scorer and prove it implements the contract before use."""
    module = importlib.import_module(module_path)
    missing = [name for name in _REQUIRED if not hasattr(module, name)]
    if missing:
        raise AttributeError(
            f"{module_path} is not a lane scorer: it defines no {', '.join(missing)}"
        )
    return module
