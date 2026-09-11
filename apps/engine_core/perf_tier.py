"""Re-export shared perf tier helpers for ``python -m apps.engine_core.perf_tier``."""

from __future__ import annotations

from apps.shared.perf_tier import *  # noqa: F403
from apps.shared.perf_tier import main

if __name__ == "__main__":
    raise SystemExit(main())
