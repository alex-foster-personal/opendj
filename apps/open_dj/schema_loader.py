"""Load + cache the open-dj JSON Schema."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from apps.open_dj import SCHEMA_VERSION

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=4)
def load_schema(version: str = SCHEMA_VERSION) -> dict[str, Any]:
    """Return the parsed open-dj JSON Schema document for ``version``."""
    path = REPO_ROOT / "open-dj" / "schema" / f"v{version}" / "open-dj.schema.json"
    if not path.exists():
        raise FileNotFoundError(
            f"open-dj schema v{version} not found at {path}."
        )
    with path.open("rb") as fh:
        return json.load(fh)


def schema_path(version: str = SCHEMA_VERSION) -> Path:
    """Absolute path to the schema file for ``version``."""
    return REPO_ROOT / "open-dj" / "schema" / f"v{version}" / "open-dj.schema.json"
