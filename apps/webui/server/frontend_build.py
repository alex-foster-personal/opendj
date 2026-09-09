"""Resolve the frontend bundle directory shared by every SPA host."""

from __future__ import annotations

import os
from pathlib import Path

FRONTEND_BUILD_DIR_ENV = "MDT_FRONTEND_BUILD_DIR"
DEFAULT_FRONTEND_BUILD_DIR = Path(__file__).resolve().parents[1] / "frontend" / "build"


def frontend_build_dir() -> Path:
    """Return the configured frontend bundle directory or the repository default."""
    configured_dir = os.environ.get(FRONTEND_BUILD_DIR_ENV)
    if configured_dir is None:
        return DEFAULT_FRONTEND_BUILD_DIR
    if not configured_dir.strip():
        raise ValueError(f"{FRONTEND_BUILD_DIR_ENV} must not be empty")
    return Path(configured_dir)
