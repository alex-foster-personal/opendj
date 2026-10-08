"""music-dj-tools web UI daemon (Phase 11).

FastAPI backend + SvelteKit static SPA served from the same process.

Entry points:
  python -m apps.webui.server                       # dev runner
  just webui-backend                                # worktree-safe runner

Requirement coverage: CAT-05, CAT-05a (UI surfaces), CAT-05b (auth posture).
"""
from __future__ import annotations

from apps.shared.app_version import APP_VERSION

# The app version from tauri.conf.json (apps/shared/app_version.py).
__version__ = APP_VERSION
__all__ = ["__version__"]
