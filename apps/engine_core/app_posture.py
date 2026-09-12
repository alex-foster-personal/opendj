"""Re-export shared app posture helpers for ``python -m apps.engine_core.app_posture``."""

from __future__ import annotations

from apps.shared.app_posture import *  # noqa: F403
from apps.shared.app_posture import main

if __name__ == "__main__":
    raise SystemExit(main())
