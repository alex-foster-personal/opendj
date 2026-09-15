"""Throttled single-flight re-arm for transient boot-time hydration failures."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import FastAPI

from apps.cloud import stem_index
from apps.cloud.stem_source import arm_stem_hydration_source
from apps.webui.server.app_wiring import _install_armed_stem_hydration

_rearm_lock = threading.Lock()
_last_rearm_attempt_mono: dict[Path, float] = {}


def maybe_rearm_stem_hydration(app: FastAPI) -> bool:
    """If transient-unarmed, attempt boot-style arming once per throttle window.

    Returns True when ``app.state`` now has a usable hydration source.
    """
    unarmed_reason = getattr(app.state, "stem_hydration_unarmed_reason", None)
    unarmed_kind = getattr(app.state, "stem_hydration_unarmed_kind", None)
    if unarmed_reason is None or unarmed_kind == "structural":
        return getattr(app.state, "stem_hydration_source", None) is not None
    data_dir = getattr(app.state, "stem_hydration_data_dir", None)
    if data_dir is None:
        return False
    data_dir = Path(data_dir)
    interval = stem_index.INDEX_REFRESH_RETRY_INTERVAL_S
    with _rearm_lock:
        now = time.monotonic()
        last = _last_rearm_attempt_mono.get(data_dir)
        if last is not None and now - last < interval:
            return False
        _last_rearm_attempt_mono[data_dir] = now
        armed = arm_stem_hydration_source(data_dir)
        if armed.source is not None:
            _install_armed_stem_hydration(
                app,
                data_dir=data_dir,
                source=armed.source,
                start_refresh_thread=False,
            )
            return True
        app.state.stem_hydration_unarmed_reason = armed.unarmed_reason
        app.state.stem_hydration_unarmed_kind = armed.unarmed_kind
        return False


__all__ = ["maybe_rearm_stem_hydration"]
