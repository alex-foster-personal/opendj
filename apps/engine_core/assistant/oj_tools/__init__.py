"""OJ's typed tools: one module per domain, collected into :data:`OJ_TOOLS`.

The core tools (``core_tools.py``) are generic: ``library_read`` takes any
``/api/v1`` path, so the model has to guess routes. A typed tool names one job
(``library_search``, ``track_get``), takes typed arguments and calls a fixed
route. ``api.py`` serves ``ALL_TOOLS = CORE_TOOLS + MACHINE_TOOLS + OJ_TOOLS``.

Adding a domain is one new file and nothing shared:

1. Create ``oj_tools/<domain>.py`` (lower-case, one word or snake_case, e.g.
   ``playlists.py``). It must export ``TOOLS: tuple[ToolSpec, ...]`` with at
   least one tool. Modules whose name starts with ``_`` are helpers
   (``_args.py``) and are never collected.
2. Every public submodule is imported in name order (``pkgutil.iter_modules``)
   and its tools appended in the order the module lists them. Import fails
   (:class:`RegistryError`) on a module with no ``TOOLS`` tuple of
   :class:`ToolSpec`, an empty one, or a tool name already exported by another
   module; the message names both modules. Names must also not repeat a core or
   machine tool (``(private path)``).

Conventions every module follows:

- Name: ``<noun>_<verb>`` in snake_case (``track_get``, ``playlist_rename``),
  so related tools sort together in the model's list.
- Description: what it does in one or two sentences, then its twins, e.g.
  ``CLI: opendj api GET /api/v1/tracks/{id}; MCP: app_state``, so agent parity
  is visible on every tool.
- Gate: ``read`` for a tool that only GETs; ``confirm`` for anything that
  writes, so the user approves it before it runs. Never ``read`` on a write.
- Engine access: only through :func:`core_tools.engine_call` (bounded body
  plus ETag) or :func:`core_tools.engine_json` (whole body, for a tool that
  projects rows to a few fields before returning them). Never ``httpx``
  directly, never engine internals.
- Arguments: validate with ``oj_tools._args`` before any call.
- Errors: raise :class:`ToolFailure` with one of the codes the loop already
  knows, never a new one for the same case:

  - ``invalid_arguments``: the call's arguments are wrong; nothing was sent.
  - ``engine_unreachable``: the engine gave no answer (from ``engine_call``).
  - ``no_performance_page``: the route needs an open page (from ``engine_call``).
  - ``engine_error``: any other non-2xx, message ``METHOD path -> status: body``.
    A 404 (unknown track or playlist) is ``engine_error`` naming 404; there is
    no separate ``not_found`` code.
  - ``command_failed``: a ``/commands`` step did not run
    (``core_tools.require_steps_succeeded``).

Tests: one ``tests/engine_core/test_oj_tools_<domain>.py`` per module, driving
the real tool ``run`` against a real engine on loopback via
``tests/engine_core/oj_tool_rig.py`` (no mocks, no model calls).
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable

from apps.engine_core.assistant.agent_contract import ToolSpec


class RegistryError(ImportError):
    """A tool module broke the registry contract; raised while the package imports."""


def collect_tools(package: str, path: Iterable[str]) -> tuple[ToolSpec, ...]:
    """Every public submodule's ``TOOLS`` in module-name order; fails on a duplicate name or a missing TOOLS."""
    owners: dict[str, str] = {}
    tools: list[ToolSpec] = []
    for info in sorted(pkgutil.iter_modules(list(path)), key=lambda module: module.name):
        if info.name.startswith("_"):
            continue
        module_name = f"{package}.{info.name}"
        specs = getattr(importlib.import_module(module_name), "TOOLS", None)
        if not isinstance(specs, tuple) or not specs or not all(isinstance(spec, ToolSpec) for spec in specs):
            raise RegistryError(f"{module_name} must export TOOLS: a non-empty tuple of ToolSpec, got {specs!r}")
        for spec in specs:
            if spec.name in owners:
                raise RegistryError(f"tool {spec.name!r} is exported by both {owners[spec.name]} and {module_name}")
            owners[spec.name] = module_name
            tools.append(spec)
    return tuple(tools)


#: Every typed OJ tool, collected once at import.
OJ_TOOLS: tuple[ToolSpec, ...] = collect_tools(__name__, __path__)

__all__ = ["OJ_TOOLS", "RegistryError", "collect_tools"]
