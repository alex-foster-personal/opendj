"""Tiny loader for ``apps/analysis/config.yaml``.  Cached by path."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH: Path = Path(__file__).resolve().parent / "config.yaml"


@lru_cache(maxsize=8)
def load_config(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else CONFIG_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


__all__ = ["CONFIG_PATH", "load_config"]
