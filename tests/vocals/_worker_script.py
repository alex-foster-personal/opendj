"""Shared path and import helpers for ``scripts/vocal_region_worker.py`` tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
VOCAL_REGION_WORKER_SCRIPT = REPO_ROOT / "scripts" / "vocal_region_worker.py"


def load_vocal_region_worker(*, module_name: str = "vocal_region_worker") -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, VOCAL_REGION_WORKER_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load worker module from {VOCAL_REGION_WORKER_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
