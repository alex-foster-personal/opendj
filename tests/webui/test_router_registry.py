"""API router registry: discovery must not change the route table or first-match winners.

The snapshot in router_table_snapshot.json was recorded on the hand-listed wiring.
Committed checks keep every snapshot row (subset only, so future route PRs need not
edit the snapshot). Shadowing and winner checks pin registration order.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.convertors import (
    FloatConvertor,
    IntegerConvertor,
    PathConvertor,
    UUIDConvertor,
)
from starlette.routing import Match, Route

from apps.webui.server import app_wiring
from tests.webui.app_fixtures import client_fixture  # noqa: F401  (pytest fixture)

_SNAPSHOT = Path(__file__).with_name("router_table_snapshot.json")
_PROBE_UUID = "123e4567-e89b-12d3-a456-426614174000"
_PATH_PARAM = re.compile(r"\{([^}:]*)(?::[^}]*)?\}")
_EXCLUDED_DISCOVERY = frozenset(
    {
        "rb_assets_audio",
        "usb_tracks_files",
        "usb_volumes_sim",
        "sync_billing_proxy",
        "telemetry",
        "vocals",
        "voice_probe",
    }
)


def _ident(route: Any) -> str:
    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        return f"{endpoint.__module__}.{endpoint.__qualname__}"
    return f"<{type(route).__name__}>"


def _rows(app: FastAPI) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for route in app.routes:
        methods = sorted(getattr(route, "methods", None) or [])
        out.append({"path": route.path, "methods": methods, "endpoint": _ident(route)})
    return sorted(out, key=lambda r: (r["path"], tuple(r["methods"]), r["endpoint"]))


def _sample_for_annotation(annotation: Any) -> str:
    if annotation is int:
        return "7"
    if annotation is float:
        return "1.5"
    if annotation is bool:
        return "true"
    if annotation is UUID:
        return _PROBE_UUID
    return "sample"


def _sample_for_convertor(convertor: Any) -> str:
    if isinstance(convertor, IntegerConvertor):
        return "7"
    if isinstance(convertor, FloatConvertor):
        return "1.5"
    if isinstance(convertor, PathConvertor):
        return "sample/leaf"
    if isinstance(convertor, UUIDConvertor):
        return _PROBE_UUID
    return "sample"


def _probe_url(route: APIRoute) -> str:
    convertors = getattr(route, "param_convertors", {}) or {}
    by_name = {p.name: p for p in route.dependant.path_params}

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in convertors:
            return _sample_for_convertor(convertors[name])
        param = by_name.get(name)
        if param is not None:
            return _sample_for_annotation(param.annotation)
        return "sample"

    return _PATH_PARAM.sub(repl, route.path)


def _scope(url: str, method: str) -> dict[str, Any]:
    return {
        "type": "http",
        "path": url,
        "root_path": "",
        "method": method,
        "headers": [],
        "query_string": b"",
    }


def _winner(app: FastAPI, url: str, method: str) -> str | None:
    full: str | None = None
    partial: str | None = None
    for route in app.router.routes:
        match, _child = route.matches(_scope(url, method))
        if match == Match.FULL and full is None:
            full = _ident(route)
        elif match == Match.PARTIAL and partial is None:
            partial = _ident(route)
    return full if full is not None else partial


def _load_snapshot() -> dict[str, Any]:
    return json.loads(_SNAPSHOT.read_text(encoding="utf-8"))


def _api_routes(app: FastAPI) -> list[APIRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute)]


def _build_snapshot(app: FastAPI) -> dict[str, Any]:
    winners: list[dict[str, Any]] = []
    for route in _api_routes(app):
        url = _probe_url(route)
        winners.extend(
            {
                "path": route.path,
                "method": method,
                "url": url,
                "winner": _winner(app, url, method),
            }
            for method in sorted(route.methods)
        )
    winners.sort(key=lambda w: (w["path"], w["method"]))
    return {"routes": _rows(app), "winners": winners}


@pytest.mark.requirement
def test_write_router_table_snapshot(client: TestClient) -> None:
    """[if] MDT_WRITE_ROUTER_SNAPSHOT=1 [then] write router_table_snapshot.json [else stop]."""
    if os.environ.get("MDT_WRITE_ROUTER_SNAPSHOT") != "1":
        return
    payload = _build_snapshot(client.app)
    _SNAPSHOT.write_text(
        json.dumps(payload, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pytest.skip("snapshot written")


@pytest.mark.requirement
def test_route_table_keeps_every_route_main_had(client: TestClient) -> None:
    """[if] app boots [then] every main snapshot route row remains [else stop]."""
    snap = _load_snapshot()["routes"]
    snap_set = {(r["path"], tuple(r["methods"]), r["endpoint"]) for r in snap}
    current = _rows(client.app)
    current_set = {(r["path"], tuple(r["methods"]), r["endpoint"]) for r in current}
    missing = snap_set - current_set
    assert not missing, "snapshot routes missing from current app:\n" + "\n".join(
        sorted(str(m) for m in missing)
    )
    snap_paths: dict[str, set[str]] = {}
    for row in snap:
        snap_paths.setdefault(row["path"], set()).add(row["endpoint"])
    rebound = [
        r
        for r in current
        if r["path"] in snap_paths and r["endpoint"] not in snap_paths[r["path"]]
    ]
    assert not rebound, "path re-bound to a different endpoint:\n" + "\n".join(
        f"{r['path']} {r['methods']}: now {r['endpoint']}" for r in rebound
    )


@pytest.mark.requirement
def test_every_main_route_is_still_answered_by_the_same_endpoint(client: TestClient) -> None:
    """[if] app boots [then] each snapshot winner still matches [else stop]."""
    mismatches: list[str] = []
    for entry in _load_snapshot()["winners"]:
        now = _winner(client.app, entry["url"], entry["method"])
        if now != entry["winner"]:
            mismatches.append(
                f"{entry['path']} {entry['method']}: was {entry['winner']}, now {now}"
            )
    assert not mismatches, "\n".join(mismatches)


@pytest.mark.requirement
def test_flagged_module_in_a_package_is_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] temp package sets MOUNT_UNDER_API_PREFIX [then] only flagged routers mount [else stop]."""
    pkg_name = "regpkg_registry_probe"
    pkg_dir = tmp_path / pkg_name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "flagged.py").write_text(
        "\n".join(
            [
                "from fastapi import APIRouter",
                "router = APIRouter()",
                "MOUNT_UNDER_API_PREFIX = True",
                '@router.get("/registry-probe")',
                "def probe(): return {'ok': True}",
            ]
        ),
        encoding="utf-8",
    )
    (pkg_dir / "unflagged.py").write_text(
        "\n".join(
            [
                "from fastapi import APIRouter",
                "router = APIRouter()",
                '@router.get("/registry-unflagged")',
                "def noop(): return {'ok': True}",
            ]
        ),
        encoding="utf-8",
    )
    (pkg_dir / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = importlib.import_module(pkg_name)
    routers = app_wiring._routers_under(pkg)
    flagged = importlib.import_module(f"{pkg_name}.flagged")
    assert routers == [flagged.router]
    mini = FastAPI()
    for router in routers:
        mini.include_router(router, prefix="/api/v1")
    paths = {r.path for r in mini.routes if isinstance(r, Route)}
    assert "/api/v1/registry-probe" in paths
    assert "/api/v1/registry-unflagged" not in paths
    for name in (pkg_name, f"{pkg_name}.flagged", f"{pkg_name}.unflagged", f"{pkg_name}.helper"):
        monkeypatch.delitem(sys.modules, name, raising=False)


@pytest.mark.requirement
def test_flag_without_router_names_the_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] flag set but no router [then] _routers_under names the module [else stop]."""
    pkg_name = "regpkg_flag_only"
    pkg_dir = tmp_path / pkg_name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "broken.py").write_text(
        "MOUNT_UNDER_API_PREFIX = True\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = importlib.import_module(pkg_name)
    with pytest.raises(TypeError, match="router"):
        app_wiring._routers_under(pkg)
    for name in (pkg_name, f"{pkg_name}.broken"):
        monkeypatch.delitem(sys.modules, name, raising=False)


@pytest.mark.requirement
def test_flag_is_exactly_the_old_hand_list() -> None:
    """[if] routes package loads [then] discovery matches the old hand list [else stop]."""
    from apps.webui.server import routes as routes_package

    routers = app_wiring._routers_under(routes_package)
    import pkgutil

    names = set()
    for info in sorted(pkgutil.iter_modules(routes_package.__path__), key=lambda i: i.name):
        mod = importlib.import_module(f"{routes_package.__name__}.{info.name}")
        if getattr(mod, "MOUNT_UNDER_API_PREFIX", False) is True:
            names.add(info.name)
    assert not names & _EXCLUDED_DISCOVERY
    assert len({id(r) for r in routers}) == len(routers)
