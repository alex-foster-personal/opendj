"""Read-only settings endpoint (settings-page).

Surfaces the daemon's effective runtime config so the frontend can render
a diagnostics page without SSH-ing into the host. Every value below is
sourced from ``request.app.state``, a middleware/route introspection, or
an ``apps.shared.paths`` constant -- see the citing comment on each field.
Nothing here is user-editable; v1 is read-only (no PATCH/POST). Runtime
policy thresholds (hide-broken ratio, ANLZ points bounds, file_exists TTL)
are published in the ``Runtime policy`` group from
``apps.shared.runtime_policy``.

Anything the running process cannot actually introspect is reported with
``tbd=true`` and a note instead of a guessed value.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount

from apps.adapters.rekordbox import config as rb_config
from apps.engine_core.host_info import HOST_INFO_STATE_ATTR
from apps.shared.paths import DATA_DIR, STATE_DIR
from apps.shared.perf_tier import HostFacts, read_override_from_prefs, resolve_tier
from apps.shared.runtime_policy import settings_items as runtime_policy_items
from apps.webui.server.frontend_build import frontend_build_dir

router = APIRouter(prefix="/settings", tags=["settings"])

_FRONTEND_BUILD_DIR = frontend_build_dir()


# ----- pydantic models (inline per router convention, see progress.py) -------

class SettingItem(BaseModel):
    key: str
    value: Any = None
    tbd: bool = False
    note: str | None = None


class SettingsGroup(BaseModel):
    group: str
    items: list[SettingItem]


class SettingsOut(BaseModel):
    groups: list[SettingsGroup]


# ----- helpers -----------------------------------------------------------

def _cors_middleware(request: Request) -> dict[str, Any] | None:
    """Pull the live CORSMiddleware kwargs off the app, if it was added.

    ``app.user_middleware`` holds each registered ``Middleware(cls, **kwargs)``
    entry (app.py:79-93); this reads the actual configured values instead of
    re-stating the README's CORS policy prose.
    """
    for mw in request.app.user_middleware:
        if mw.cls is CORSMiddleware:
            return dict(mw.kwargs)
    return None


def _frontend_mounted(request: Request) -> bool:
    """True if app.py mounted the built SPA (app.py:114-116, name="spa")."""
    return any(
        isinstance(r, Mount) and getattr(r, "name", None) == "spa"
        for r in request.app.routes
    )


# ----- route ---------------------------------------------------------------

@router.get("", response_model=SettingsOut)
def get_settings(request: Request) -> SettingsOut:
    state = request.app.state
    cors = _cors_middleware(request)

    network_items = [
        SettingItem(key="bind_host", value=getattr(state, "bind_host", None),
                    note="MUSIC_DJ_BIND_HOST env var; default 127.0.0.1 (D5/CAT-05b)."),
        SettingItem(key="hostname", value=getattr(state, "hostname", None)),
        SettingItem(
            key="port", value=getattr(state, "port", None),
            tbd=getattr(state, "port", None) is None,
            note=(
                "Effective CLI --port or MUSIC_DJ_BACKEND_PORT from the "
                "worktree root .env."
            ),
        ),
        SettingItem(
            key="cors_enabled", value=cors is not None,
            note="Whether CORSMiddleware is registered (app.py enable_cors).",
        ),
        SettingItem(
            key="cors_allow_origins",
            value=(cors or {}).get("allow_origins") if cors else None,
            tbd=cors is None,
            note="Live CORSMiddleware allow_origins; see apps/webui/README.md CORS policy.",
        ),
    ]

    backend_items = [
        SettingItem(
            key="backend_type", value=type(getattr(state, "backend", None)).__name__,
            note="InMemoryBackend or SqliteBackend (apps/webui/server/sqlite_backend.py).",
        ),
        SettingItem(key="state_db_path", value=str(getattr(state, "state_db_path", None))),
        SettingItem(key="version", value=getattr(state, "version", None)),
    ]

    storage_items = [
        SettingItem(key="data_dir", value=str(DATA_DIR),
                    note="apps.shared.paths.DATA_DIR"),
        SettingItem(key="state_dir", value=str(STATE_DIR),
                    note="apps.shared.paths.STATE_DIR"),
        SettingItem(key="anlz_cache_dir", value=str(rb_config.ANLZ_CACHE_DIR),
                    note="apps.adapters.rekordbox.config.ANLZ_CACHE_DIR"),
    ]

    frontend_items = [
        SettingItem(key="frontend_mounted", value=_frontend_mounted(request),
                    note="Whether apps/webui/frontend/build is mounted as the SPA at /."),
        SettingItem(key="frontend_build_dir", value=str(_FRONTEND_BUILD_DIR)),
        SettingItem(
            key="vibe_sensitivity",
            value=0.07,
            note="Pointer-travel charge multiplier used by the Vibe meter.",
        ),
        SettingItem(
            key="vibe_decay_per_sec",
            value=0.05,
            note="Linear Vibe charge decay per second while idle.",
        ),
    ]

    host_identity = getattr(state, HOST_INFO_STATE_ATTR, None)
    machine_perf_tier: SettingItem
    if host_identity is not None and host_identity.logical_cpus is not None and host_identity.ram_bytes is not None:
        facts = HostFacts(
            logical_cpus=host_identity.logical_cpus,
            ram_bytes=host_identity.ram_bytes,
        )
        override = read_override_from_prefs(DATA_DIR)
        resolved = resolve_tier(facts=facts, override=override)
        machine_perf_tier = SettingItem(
            key="machine_perf_tier",
            value=resolved.value,
            note=(
                "Resolved LOW/STANDARD/HIGH from GET /api/v1/perf-tier and "
                "ui-prefs perf_tier (Auto/Low/Standard/High)."
            ),
        )
    elif host_identity is not None and host_identity.failure is not None:
        machine_perf_tier = SettingItem(
            key="machine_perf_tier",
            tbd=True,
            note=host_identity.failure,
        )
    else:
        machine_perf_tier = SettingItem(
            key="machine_perf_tier",
            tbd=True,
            note="engine chassis did not resolve host-info",
        )

    toggle_items = [
        SettingItem(
            key="feature_toggles",
            tbd=False,
            note=(
                "First machine-tier flag: GET /api/v1/perf-tier plus ui-prefs "
                "perf_tier (Auto/Low/Standard/High), not an env var."
            ),
        ),
        machine_perf_tier,
    ]

    return SettingsOut(groups=[
        SettingsGroup(group="Network", items=network_items),
        SettingsGroup(group="Backend", items=backend_items),
        SettingsGroup(group="Storage", items=storage_items),
        SettingsGroup(group="Frontend", items=frontend_items),
        SettingsGroup(
            group="Runtime policy",
            items=[
                SettingItem(key=item.key, value=item.value, note=item.note)
                for item in runtime_policy_items()
            ],
        ),
        SettingsGroup(group="Feature toggles", items=toggle_items),
    ])


__all__ = ["SettingItem", "SettingsGroup", "SettingsOut", "router"]
