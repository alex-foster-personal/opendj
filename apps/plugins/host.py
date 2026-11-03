"""Load enabled modules at engine start, and the seam they register through.

A first-party module's ``register(host)`` is called once, at boot, with a
:class:`PluginHost`. Through it, and only through it, a module adds:

* HTTP routers, every path under one of its declared ``api_prefixes``
  (mounted below ``/api/v1``), and never over a route the core already has;
* startup and shutdown hooks, run by the engine lifespan;
* nothing else. UI code is served from the module's own files and mounted
  by the SPA into named slots (``ui.slots``); it needs no Python.

EVERY OUTCOME IS REPORTED, NOTHING IS SKIPPED SILENTLY. Each enabled plugin
ends in exactly one :class:`LoadOutcome` status, which the store page and
``GET /api/v1/plugins`` show: ``loaded``, ``blocked`` (signed blocklist),
``dev-only`` (a sideload while developer mode is off), ``tampered``,
``conflict`` (a package or route the core already owns), ``missing-dependency``,
``failed`` (register raised), ``no-consumer`` (a data plugin this build has
nothing to read yet, so it is installed but not claimed to be running). One broken module does not stop the engine: the
plugin is optional by definition, and a DJ whose B2B module broke must still
get their decks. That is the one deliberate exception to fail-fast here, and
it is loud (PLUG-6522e).

BOOT COST. Loading re-hashes each enabled module's files once
(:func:`apps.plugins.installer.verify_installed`); with no plugin enabled it
reads one small JSON file and does nothing else.
"""

from __future__ import annotations

import atexit
import importlib
import importlib.util
import inspect
import itertools
import logging
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib.machinery import all_suffixes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from starlette.routing import compile_path

from apps.plugins.catalog import Catalog
from apps.plugins.installer import (
    InstallError,
    check_app_version,
    dev_mode,
    read_installed_manifest,
    snapshot_python,
    verify_installed,
    verify_receipt,
)
from apps.plugins.manifest import PluginManifest, PythonEntry
from apps.plugins.state import InstalledPlugin, PluginState
from apps.shared.app_version import APP_VERSION

log = logging.getLogger(__name__)

Hook = Callable[[], Any]
# What an APIRouter given no lifespan carries; anything else is a lifespan the engine would never run.
_NO_LIFESPAN: type = type(APIRouter().lifespan_context)

_PARAM_RE = re.compile(r"\{[^}:]*(?::([^}]*))?\}")


def route_key(path: str) -> str:
    """A path as Starlette matches it: ``/x/{id}`` and ``/x/{name}`` are one route.

    The converter is kept (``str`` when none is named), so ``/x/{id:int}`` and
    ``/x/{slug:str}`` stay distinct: Starlette reaches each for its own segments.
    """
    return _PARAM_RE.sub(lambda m: "{:" + (m.group(1) or "str") + "}", path)


class PluginRegistrationError(ValueError):
    pass


@dataclass
class PluginHost:
    """What one module may touch. Built per module by :func:`load_enabled`."""

    manifest: PluginManifest
    data_dir: Path
    routers: list[Any] = field(default_factory=list)
    startup_hooks: list[Hook] = field(default_factory=list)
    shutdown_hooks: list[Hook] = field(default_factory=list)

    @property
    def plugin_id(self) -> str:
        return self.manifest.plugin_id

    def include_router(self, router: Any) -> None:
        """Queue a FastAPI ``APIRouter``; every route must sit under a declared prefix."""
        self._check_router(router)
        self.routers.append(router)

    def seal_routers(self) -> None:
        """Re-check each router as ``register`` left it, then keep a copy of its routes.

        ``include_router`` checks a router when it is queued, but the plugin
        still holds it and could add a raw route, an out-of-prefix path or a
        lifecycle hook afterwards. The copy is what gets mounted.
        """
        sealed = []
        for router in self.routers:
            self._check_router(router)
            copy = APIRouter()
            copy.routes.extend(router.routes)
            sealed.append(copy)
        self.routers = sealed

    def _check_router(self, router: Any) -> None:
        if not isinstance(router, APIRouter):
            raise PluginRegistrationError(
                f"{self.plugin_id}: include_router takes a fastapi.APIRouter, not {type(router).__name__}"
            )
        if router.on_startup or router.on_shutdown or not isinstance(router.lifespan_context, _NO_LIFESPAN):
            # The engine has its own lifespan, so FastAPI would never run these:
            # the routes would be live while the plugin never initialized.
            raise PluginRegistrationError(
                f"{self.plugin_id}: register startup and shutdown with host.on_startup and host.on_shutdown, "
                "not on the router"
            )
        prefixes = self.manifest.python.api_prefixes if self.manifest.python else ()
        for route in getattr(router, "routes", []):
            path = getattr(route, "path", "")
            if not isinstance(route, APIRoute):
                # Only an APIRoute takes the include-time dependency that closes a
                # plugin's routes when its startup fails; a raw Route, websocket or
                # mount would stay reachable in a plugin that never initialized.
                raise PluginRegistrationError(
                    f"{self.plugin_id}: route {path!r} is a {type(route).__name__}; plugins may add only "
                    "FastAPI HTTP routes (@router.get, @router.post, ...)"
                )
            if not any(path == p or path.startswith(p + "/") for p in prefixes):
                raise PluginRegistrationError(
                    f"{self.plugin_id}: route {path!r} is outside its declared api_prefixes {list(prefixes)}"
                )

    def _sync_hook(self, hook: Hook) -> Hook:
        # The engine runs hooks synchronously; an async def would only create
        # a coroutine and report success without running.
        if inspect.iscoroutinefunction(hook):
            raise PluginRegistrationError(f"{self.plugin_id}: lifecycle hooks must be plain functions, not async")
        return hook

    def on_startup(self, hook: Hook) -> None:
        self.startup_hooks.append(self._sync_hook(hook))

    def on_shutdown(self, hook: Hook) -> None:
        self.shutdown_hooks.append(self._sync_hook(hook))


@dataclass(frozen=True)
class LoadOutcome:
    plugin_id: str
    version: str
    status: str
    detail: str = ""


@dataclass
class PluginRuntime:
    """What this boot loaded. Lives on ``app.state.plugins``."""

    plugins_dir: Path
    outcomes: dict[str, LoadOutcome] = field(default_factory=dict)
    hosts: dict[str, PluginHost] = field(default_factory=dict)
    manifests: dict[str, PluginManifest] = field(default_factory=dict)
    #: The install id each record carried at boot, so a later reinstall of the
    #: same version still reads as waiting for a restart.
    install_ids: dict[str, str] = field(default_factory=dict)

    def loaded(self, plugin_id: str) -> bool:
        outcome = self.outcomes.get(plugin_id)
        return outcome is not None and outcome.status == "loaded"

    def run_startup(self) -> None:
        """Run startup hooks; the first failure marks the plugin failed, which closes its routes."""
        for plugin_id, host in self.hosts.items():
            for hook in host.startup_hooks:
                try:
                    _call_sync(hook)
                except Exception as exc:
                    log.exception("plugin %s startup hook failed", plugin_id)
                    self.outcomes[plugin_id] = LoadOutcome(
                        plugin_id, host.manifest.version, "failed", f"startup hook: {exc}"
                    )
                    break

    def run_shutdown(self) -> None:
        for plugin_id, host in self.hosts.items():
            for hook in reversed(host.shutdown_hooks):
                try:
                    _call_sync(hook)
                except Exception:
                    log.exception("plugin %s shutdown hook failed", plugin_id)


def _call_sync(hook: Hook, what: str = "lifecycle hook") -> None:
    """Call ``hook`` and refuse an awaitable result, e.g. from ``lambda: warm_up()``.

    An ``async def`` would only build a coroutine here and report success without running.
    """
    try:
        result = hook()
    except SystemExit as exc:
        # A plugin may not stop the engine: sys.exit() is one more failure of that plugin.
        raise PluginRegistrationError(f"{what} called sys.exit({exc.code!r})") from exc
    if inspect.isawaitable(result):
        close = getattr(result, "close", None)
        if callable(close):
            close()  # never awaited; closing it avoids a "never awaited" warning
        raise PluginRegistrationError(f"{what} returned an awaitable; it must be a plain function")


def _apps_package_path() -> list[str]:
    import apps

    return apps.__path__  # type: ignore[return-value]


def _core_owns(package: str, own: Path) -> bool:
    """True when ``apps.<package>`` exists anywhere other than this plugin's copies under ``own``.

    A folder without ``__init__.py`` (``apps/desktop``) is a namespace package:
    its spec has no origin, and a plugin's regular package of the same name
    would shadow it, so every other ``apps`` path entry is checked for the name.
    """
    for entry in _apps_package_path():
        base = Path(entry)
        if base.is_relative_to(own):
            continue
        if (base / package).is_dir() or any((base / f"{package}{suffix}").is_file() for suffix in all_suffixes()):
            return True
    spec = importlib.util.find_spec(f"apps.{package}")
    if spec is None:
        return False
    places = [spec.origin] if spec.origin else list(spec.submodule_search_locations or [])
    return any(not Path(place).is_relative_to(own) for place in places)


def _load_one(
    plugins_dir: Path,
    data_dir: Path,
    manifest: PluginManifest,
    taken: list[tuple[str, str]],
) -> PluginHost:
    host = PluginHost(manifest=manifest, data_dir=data_dir / "plugin-data" / manifest.plugin_id)
    python = manifest.python
    if python is None:
        return host
    # Import from a private copy made while hashing: the install tree stays
    # writable, and a file swapped in after verify_installed must never run.
    mine = _snapshot_root() / manifest.plugin_id
    mine.mkdir(exist_ok=True)
    snapshot = Path(tempfile.mkdtemp(dir=mine))
    try:
        python_root = str(snapshot_python(plugins_dir, manifest, snapshot))
        _check_imports(python, mine)
    except BaseException:
        # Refused before import: a package named like a core one must not unload the core's modules.
        shutil.rmtree(snapshot, ignore_errors=True)
        raise
    apps_path = _apps_package_path()
    if python_root not in apps_path:
        apps_path.append(python_root)
    try:
        module_name, _, func_name = python.register.partition(":")
        register = getattr(importlib.import_module(module_name), func_name)
        host.data_dir.mkdir(parents=True, exist_ok=True)
        _call_sync(lambda: register(host), what=f"register {python.register}")
        claims = _claim_routes(host, taken)
        _check_schema(host)
        taken.extend(claims)  # only a module that will mount holds its routes
        _RESIDENT.append((manifest, python_root))
    except BaseException:
        # A plugin reported as failed must leave nothing importable behind:
        # neither its package root on apps.__path__ nor its loaded modules.
        if python_root in apps_path:
            apps_path.remove(python_root)
        _unload_packages(manifest)
        shutil.rmtree(snapshot, ignore_errors=True)
        raise
    return host


_SNAPSHOT_ROOTS: list[Path] = []
# Plugins this process has imported: a later app in the same interpreter must not reuse their modules.
_RESIDENT: list[tuple[PluginManifest, str]] = []


def _forget_resident() -> None:
    """Drop every plugin module a previous app in this process imported, and its snapshot path."""
    apps_path = _apps_package_path()
    for manifest, python_root in _RESIDENT:
        _unload_packages(manifest)
        if python_root in apps_path:
            apps_path.remove(python_root)
    _RESIDENT.clear()


def _snapshot_root() -> Path:
    """This process's private folder of verified Python copies; loaded code runs from it until exit."""
    if not _SNAPSHOT_ROOTS:
        root = Path(tempfile.mkdtemp(prefix="odj-plugins-"))
        atexit.register(shutil.rmtree, root, True)
        _SNAPSHOT_ROOTS.append(root)
    return _SNAPSHOT_ROOTS[0]


def _check_imports(python: PythonEntry, own: Path) -> None:
    """Refuse a package the build already has, or a dependency it lacks."""
    for package in python.packages:
        if _core_owns(package, own):
            raise InstallError("conflict", f"apps.{package} already exists in this build")
    for name in python.requires:
        if importlib.util.find_spec(name) is None:
            raise InstallError(
                "missing-dependency", f"needs Python package {name!r}, which this build does not include"
            )


def _claim_routes(host: PluginHost, taken: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Refuse a route that overlaps one the core, the store or an earlier plugin serves; return the claims.

    The caller adds them to ``taken`` once the module passes every other check.

    Mount order differs (plugins go in before the store's router), so a
    conflict is either route matching every request of the other for one
    method: ``/plugins/{name}`` would swallow the store's ``/plugins/ui-slots``.
    """
    host.seal_routers()
    routes = [r for router in host.routers for r in router.routes]
    mine = [(method, "/api/v1" + getattr(r, "path", "")) for r in routes for method in _methods(r)]
    for method, path in mine:
        for other_method, other in taken:
            if _same_method(method, other_method) and (_shadows(other, path) or _shadows(path, other)):
                raise InstallError(
                    "conflict", f"{method} {path} overlaps {other}, already served by the core or another plugin"
                )
    _check_own_routes(routes)
    return mine


def _check_schema(host: PluginHost) -> None:
    """Refuse routers whose OpenAPI schema cannot be generated.

    The engine builds its whole schema after every plugin is mounted, outside
    any one plugin's isolation, so one bad response model there would stop
    every boot; generating the plugin's own schema first fails only it.
    """
    probe = FastAPI()
    for router in host.routers:
        probe.include_router(router, prefix="/api/v1")
    try:
        probe.openapi()
    except Exception as exc:
        raise InstallError("bad-schema", f"its API schema cannot be generated: {type(exc).__name__}: {exc}") from exc


def _methods(route: Any) -> list[str]:
    """A route's HTTP methods; ``*`` for one that takes any (a websocket or a mount)."""
    return sorted(getattr(route, "methods", None) or {"*"})


def _same_method(a: str, b: str) -> bool:
    return a == b or "*" in (a, b)


def taken_routes(routes: Iterable[Any], prefix: str = "") -> list[tuple[str, str]]:
    """``(method, path)`` for each route already in the app, as ``load_enabled`` takes them.

    A mount (static files, a sub-app) answers everything under its path.
    """
    out: list[tuple[str, str]] = []
    for route in routes:
        path = prefix + getattr(route, "path", "")
        if not hasattr(route, "methods") and hasattr(route, "routes"):
            path = path.rstrip("/") + "/{rest:path}"
        out.extend((method, path) for method in _methods(route))
    return out


def _check_own_routes(routes: list[Any]) -> None:
    """Refuse a plugin route that one of its earlier routes leaves unreachable.

    Starlette sends a request to the first route that matches, so ``GET
    /x/{name}`` before ``GET /x/special`` (or ``{v:str}`` before ``{v:int}``)
    loads with the second never called. The reverse orders are fine, as is one
    path with different methods.
    """
    earlier: dict[str, list[str]] = {}
    for route in routes:
        path = "/api/v1" + getattr(route, "path", "")
        for method in _methods(route):
            for before in earlier.get(method, []):
                if _shadows(before, path):
                    raise InstallError(
                        "conflict", f"{method} {path} is unreachable: this plugin's earlier route {before} matches it"
                    )
            earlier.setdefault(method, []).append(path)


#: Values each converter matches, standing in for "every request" of a route
#: template: a route shadows a later one when its compiled matcher accepts all of
#: them. ``str`` mixes letters, digits and a dot so ``{n:int}`` or ``{x}.json``
#: before ``{name}`` is not read as covering it.
_CONVERTER_SAMPLES: dict[str, tuple[str, ...]] = {
    "str": ("a", "x-1.b", "123"),
    "path": ("a", "a/b.c"),
    "int": ("0", "42"),
    "float": ("1.5", "2"),
    "uuid": ("123e4567-e89b-12d3-a456-426614174000",),
}
#: Beyond this many combinations, sample each parameter on its own instead.
_MAX_SAMPLES = 256
_TEMPLATE_PARAM = re.compile(r"\{[^}:]*(?::([^}]*))?\}")


def _request_samples(path: str) -> list[str] | None:
    """Concrete paths ``path`` matches, one per combination of converter samples.

    None when it uses a converter without samples; the caller then compares templates.
    """
    parts = _TEMPLATE_PARAM.split(path)  # literal, converter, literal, converter, ...
    choices: list[tuple[str, ...]] = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            choices.append((part,))
            continue
        samples = _CONVERTER_SAMPLES.get(part or "str")
        if samples is None:
            return None
        choices.append(samples)
    combos = 1
    for options in choices:
        combos *= len(options)
    if combos <= _MAX_SAMPLES:
        return ["".join(combo) for combo in itertools.product(*choices)]
    # Too many to try all: the first sample everywhere, then each parameter varied
    # alone, so every parameter's every sample is tried (a prefix of the product
    # would only ever vary the last parameters).
    base = [options[0] for options in choices]
    return ["".join(base)] + [
        "".join([*base[:i], option, *base[i + 1:]])
        for i, options in enumerate(choices)
        for option in options[1:]
    ]


def _shadows(first: str, later: str) -> bool:
    """Whether route ``first`` matches every request route ``later`` would.

    ``first`` is compiled by Starlette itself, so mixed segments such as
    ``{stem}.json`` and every converter it knows match exactly as they will
    at runtime.
    """
    samples = _request_samples(later)
    try:
        matcher = compile_path(first)[0]
    except (AssertionError, KeyError, ValueError):
        matcher = None
    if samples is None or matcher is None:
        return route_key(first) == route_key(later)
    return all(matcher.match(sample) is not None for sample in samples)


def _unload_packages(manifest: PluginManifest) -> None:
    if manifest.python is None:
        return
    for package in manifest.python.packages:
        prefix = f"apps.{package}"
        for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
            del sys.modules[name]


def _verified_manifest(
    plugins_dir: Path, installed: InstalledPlugin, trusted: Mapping[str, str] | None, app_version: str,
    labs_opt_in: bool,
) -> PluginManifest:
    """The installed manifest, its files re-hashed and, for a store install, its signed receipt checked.

    A verified data plugin ends here as ``no-consumer``: nothing in this build
    reads one yet, so reporting it ``loaded`` would claim an effect it has not.
    """
    manifest = read_installed_manifest(plugins_dir, installed.plugin_id, installed.version)
    verify_installed(plugins_dir, manifest)
    if installed.source.startswith("store:"):
        verify_receipt(plugins_dir, manifest, installed.source.removeprefix("store:"), trusted=trusted)
    check_app_version(manifest, app_version)
    _check_consent(installed, manifest)
    if manifest.channel == "labs" and not labs_opt_in:  # a labs sideload too, not only store:labs
        raise InstallError("labs-not-enabled", "a labs module; turn on the labs channel to load it")
    if manifest.kind == "data":
        raise InstallError("no-consumer", "installed and verified; nothing in this build reads this plugin yet")
    return manifest


def _check_consent(installed: InstalledPlugin, manifest: PluginManifest) -> None:
    """Refuse a manifest whose permissions or terms notice the recorded consent does not cover.

    Install records consent for the manifest it accepted; files and records can
    still drift apart (a restored ``state.json``, a rebuild at the same version),
    so boot checks the pair again rather than trusting install time (fail closed).
    """
    if sorted(installed.granted_permissions) != sorted(manifest.permissions):
        raise InstallError(
            "permissions-not-accepted",
            f"the installed build asks for {sorted(manifest.permissions)} but "
            f"{sorted(installed.granted_permissions)} were accepted; reinstall it to review them",
        )
    accepted = installed.notice_acknowledged and installed.accepted_notice_sha256 == manifest.notice_sha256
    if manifest.needs_terms_notice and not accepted:
        raise InstallError(
            "notice-not-acknowledged",
            "its third-party terms notice was never acknowledged, or changed since; reinstall it to review it",
        )


def _unchecked(installed: InstalledPlugin, checked: set[str], required: Iterable[str]) -> str | None:
    """The channel whose verified blocklist a store plugin lacks, or None.

    Its own channel always counts, and so does every channel that has verified
    here before: a revocation signed in either must not vanish with its cache.
    """
    if not installed.source.startswith("store:"):
        return None
    needed = [installed.source.removeprefix("store:"), *sorted(required)]
    return next((channel for channel in needed if f"store:{channel}" not in checked), None)


def load_enabled(  # noqa: PLR0913 - each keyword is one independent boot input.
    plugins_dir: Path,
    data_dir: Path,
    state: PluginState,
    *,
    catalogs: Iterable[Catalog] = (),
    taken: Iterable[tuple[str, str]] = (),
    environ: Mapping[str, str] | None = None,
    trusted: Mapping[str, str] | None = None,
    app_version: str = APP_VERSION,
    held: Mapping[str, str] | None = None,
) -> PluginRuntime:
    """Load every enabled plugin; record exactly one outcome for each.

    ``held`` names plugins whose interrupted install boot could not finish or
    undo, with why; they stay off (``recovery-failed``) until a later boot can.

    ``catalogs`` are the verified cached catalogs; their blocklists apply. A
    store plugin from a channel with no catalog here stays off: there is no
    blocklist to clear it against, and an empty one is not the same as none
    (fail closed).
    """
    _forget_resident()
    runtime = PluginRuntime(
        plugins_dir=plugins_dir, install_ids={p.plugin_id: p.install_id for p in state.plugins.values()},
    )
    verified = tuple(catalogs)
    blocked = tuple(b for catalog in verified for b in catalog.blocklist)
    checked = {f"store:{catalog.channel}" for catalog in verified}
    claimed = list(taken)
    developer = dev_mode(environ)
    for plugin_id, installed in sorted(state.plugins.items()):
        if not installed.enabled:
            continue
        version = installed.version

        def _record(status: str, detail: str = "", *, _id: str = plugin_id, _v: str = version) -> None:
            runtime.outcomes[_id] = LoadOutcome(_id, _v, status, detail)

        if held and plugin_id in held:
            _record("recovery-failed", held[plugin_id])
            continue
        hit = next((b for b in blocked if b.blocks(plugin_id, version)), None)
        if hit is not None:
            _record("blocked", hit.reason)
            continue
        missing = _unchecked(installed, checked, state.blocklist_channels)
        if missing is not None:
            _record("no-catalog", f"the {missing} catalog, whose blocklist it must clear, is missing or does "
                    "not verify; refresh the store, then restart the engine")
            continue
        if installed.source == "sideload" and not developer:
            _record("dev-only", "sideloaded (unsigned); start the engine with MDT_PLUGIN_DEV=1 to load it")
            continue
        try:
            manifest = _verified_manifest(plugins_dir, installed, trusted, app_version, state.labs_opt_in)
            runtime.manifests[plugin_id] = manifest
            host = _load_one(plugins_dir, data_dir, manifest, claimed)
        except InstallError as exc:
            _record(exc.code, str(exc))
            continue
        except (Exception, SystemExit) as exc:  # sys.exit() at import must not stop the boot
            log.exception("plugin %s failed to load", plugin_id)
            _record("failed", f"{type(exc).__name__}: {exc}")
            continue
        runtime.hosts[plugin_id] = host
        _record("loaded")
    return runtime
