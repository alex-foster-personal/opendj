"""music-dj-tools web UI daemon (Phase 11).

FastAPI backend + SvelteKit static SPA served from the same process.

Entry points:
  python -m apps.webui.server                       # dev runner
  uvicorn apps.webui.server.app:app --port 8585     # manual uvicorn

Requirement coverage: CAT-05, CAT-05a (UI surfaces), CAT-05b (auth posture).
"""
from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
