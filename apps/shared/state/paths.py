"""Re-export the state-layer path constants from ``apps.shared.paths``.

Local import shim so downstream code can say ``from apps.shared.state import
paths`` without cross-package imports. Any change to the canonical location
goes in ``apps/shared/paths.py`` (single source of truth).
"""
from __future__ import annotations

from apps.shared.paths import DATA_DIR, PROJECT_ROOT, STATE_DB, STATE_DIR

__all__ = ["DATA_DIR", "PROJECT_ROOT", "STATE_DB", "STATE_DIR"]
