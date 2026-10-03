"""Worktree-local Web UI port configuration and reservation manager.

Requirements:

- ✔︎ One ignored root ``.env`` owns the backend and frontend port values.
- ✔︎ The API proxy target is derived from the backend port.
- ✔︎ A Git-common-dir registry prevents copied worktrees from sharing ports.
- ✔︎ Port availability checks are per service and use cross-platform sockets.
- ✔︎ CLI values override process environment, which overrides root ``.env``.
- ✔︎ The CLI refuses to run when the loaded ``apps`` tree is another worktree.
- ✔︎ ``MUSIC_DJ_PORT_LANE`` shifts one CI runner's whole pool window clear of
  every other lane's, so independent runner clones on one host cannot select
  an overlapping pair.
- ✔︎ Fixed-port CI/desktop suites (playwright/vite/wdio configs that bind a
  compile-time port outside this module's registry) are never handed out by
  dynamic allocation, in any lane, so an ordinary worktree claim cannot land
  on one by coincidence (issue #1613).
- ✔︎ Every dynamic claim leaves a world-readable ownership marker keyed by
  port, so a process running as a DIFFERENT unix user on the same host (a
  self-hosted CI runner sharing the box with this fleet's worktrees) can name
  the claimant instead of reporting an unreadable ``/proc`` entry.
- ✔︎ A worktree's CONFIGURED pair (from ``.env``, or an explicit request) is
  kept when that worktree's own backend already holds it, exactly as the
  REMEMBERED registry pair already is, so a config reload cannot evict a
  worktree from ports its own running engine is serving (issue observed on
  the Air's preview worktree, 8728/9448 -> 8700/9420, Fri 11 Sep 2026).

Acceptance tests:

- [if] two worktrees claim a copied ``.env`` [then ⛔️] they receive one pair.
- [if] an unrelated backend owns the reserved port [then ⛔️] Vite starts.
- [if] a port is missing, malformed, privileged, or out of range [then ⛔️]
  startup continues.
- [if] ``uv`` binds an ancestor worktree's ``.venv`` [then ⛔️] the CLI prints
  ports, because those ports belong to the other worktree's ``.env``.
- [if] two runner clones claim distinct ``MUSIC_DJ_PORT_LANE`` values [then
  ⛔️] their allocated pairs overlap.
- [if] dynamic allocation, in any lane, returns a port a fixed-port suite
  binds directly [then ⛔️] the pair is returned or restored.
- [if] a claimed port has no ownership marker afterward [then ⛔️] a foreign
  user's process on it can be named.
- [if] a worktree's configured pair has no registry entry yet and that
  worktree's own backend already listens on it [then ⛔️] the claim moves it
  to a fresh pair instead of keeping the configured one.
- [if] a genuinely foreign process holds the configured pair [then ⛔️] the
  claim keeps that pair rather than moving off it.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

BACKEND_ENV = "MUSIC_DJ_BACKEND_PORT"
FRONTEND_ENV = "MUSIC_DJ_FRONTEND_PORT"
OBSOLETE_PROXY_ENV = "MUSIC_DJ_API_PROXY_TARGET"
PORT_LANE_ENV = "MUSIC_DJ_PORT_LANE"
MIN_PORT = 1024
MAX_PORT = 65535
POOL_SIZE = 120
LOCK_TIMEOUT_SECONDS = 2.0
BACKEND_POOL_START = 8680
FRONTEND_POOL_START = 9400
# Each CI runner host is a separate clone with its own common dir, so its own
# worktree-ports.json registry and lock (issue #1301): two runners' registries
# never contend, and both start allocating from slot 0 of the same pool. A
# lane shifts one runner's ENTIRE pool window (not just its first pick) clear
# of every other lane's window, so no two runners can select an overlapping
# pair regardless of what each registry already holds.
#
# The stride must clear the gap between the two pools plus one full pool
# width, or a later lane's backend window walks into an earlier lane's
# frontend window. Derived, not hardcoded, so a future change to POOL_SIZE or
# either pool start cannot silently reopen that overlap.
PORT_LANE_STRIDE = (FRONTEND_POOL_START - BACKEND_POOL_START) + POOL_SIZE
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEBUI_ENV_FILE = PROJECT_ROOT / ".env"

# Fixed E2E/desktop suite ports (issue #1613): each is a COMPILE-TIME constant
# a Playwright/Vite/WDIO config binds directly (e.g.
# apps/webui/frontend/tests/e2e/playwright.webkit-deckload.config.ts's
# WEBKIT_DECKLOAD_PORT), never through this module's registry, so the dynamic
# allocator has no way to see one occupied. Several sit inside a lane's pool
# window by coincidence (backend 8680-8799, frontend 9400-9519 for lane 0):
# an ordinary worktree claim with no MUSIC_DJ_PORT_LANE set -- a developer
# machine, or a dispatcher worker fleet worktree on a host that also runs a
# self-hosted CI runner -- could legitimately allocate one of these and
# collide with whichever suite owns it, under a DIFFERENT unix user than the
# runner, so neither side's /proc can attribute the other's holder. Excluding
# them here removes the collision by construction. Each per-suite
# ``RESERVED_PORTS``/``RESERVED`` list documents that value's own provenance;
# this set is the single place they are all excluded from allocation.
RESERVED_FIXED_PORTS: frozenset[int] = frozenset(
    {
        4455,  # apps/desktop/wdio.conf.ts EMBEDDED_WEBDRIVER_PORT
        4456,  # apps/desktop/mcp/smoke.ts WEBDRIVER_PORT
        5214,  # tests/e2e/vite.play-analytics.config.ts frontend port
        5216,  # tests/e2e/playwright.desktop-setup.config.ts SETUP_PAGE_PORT
        5228,  # tests/e2e/vite.library-wheel.config.ts frontend port
        5273,  # tests/e2e/vite.performance.config.ts DEFAULT_FRONTEND_BASE
        5277,  # tests/e2e/vite.library-jobs.config.ts LIBRARY_JOBS_E2E_FRONTEND_PORT
        5278,  # tests/e2e/vocals-demucs-overlay-endpoints.ts default frontend port
        5311,  # tests/e2e/playwright.stretch-artifact.config.ts STRETCH_ARTIFACT_PORT
        5320,  # tests/e2e/vite.hotcue-mapping-gate.config.ts frontend port
        5321,  # tests/e2e/vite.comment-hotkey-gate.config.ts / playwright.preflight-gate.config.ts
        5322,  # tests/e2e/playwright.meter-artifact.config.ts METER_ARTIFACT_PORT
        5323,  # tests/e2e/playwright.preflight-gate.config.ts / vite.full-reload-gate.config.ts
        5324,  # tests/e2e/vite.autoplay-stall-gate.config.ts / playwright.audio-soak.config.ts
        5326,  # tests/e2e/vite.autoplay-error-hunt.config.ts AUTOPLAY_HUNT_FRONTEND_PORT
        5328,  # tests/e2e/vite.lyrics-words.config.ts frontend port
        5331,  # tests/e2e/playwright.cloudsync-ui.config.ts CLOUDSYNC_UI_FRONTEND_PORT
        5399,  # tests/e2e/vite.rekordbox-gate.config.ts REKORDBOX_GATE_E2E_PORT
        8686,  # tests/e2e/vite.performance.config.ts DEFAULT_API_BASE
        8688,  # tests/e2e/stems-e2e-endpoints.ts DEFAULT_BACKEND_PORT
        8690,  # tests/e2e/playwright.webkit-deckload.config.ts WEBKIT_DECKLOAD_PORT
        8691,  # apps/desktop/wdio.conf.ts ENGINE_PORT
        8692,  # tests/e2e/playwright.boot-burst.config.ts BOOT_BURST_PORT
        8700,  # tests/e2e/playwright.stem-decode-bench.config.ts STEM_DECODE_BENCH_PORT
        8713,  # tests/e2e/playwright.playlist-switch-latency.config.ts PLAYLIST_SWITCH_BENCH_PORT
        8695,  # tests/e2e/vite.hotcue-mapping-gate.config.ts API port
        8696,  # tests/e2e/vite.comment-hotkey-gate.config.ts / playwright.preflight-gate.config.ts
        8697,  # tests/e2e/playwright.preflight-gate.config.ts PREFLIGHT_GATE_BROKEN_API_PORT
        8698,  # apps/desktop/mcp/smoke.ts ENGINE_PORT
        8699,  # tests/e2e/vite.autoplay-stall-gate.config.ts API port
        8703,  # tests/e2e/vite.autoplay-error-hunt.config.ts AUTOPLAY_HUNT_API_PORT
        8704,  # tests/e2e/library-jobs-e2e-endpoints.ts LIBRARY_JOBS_E2E_BACKEND_PORT
        8705,  # tests/e2e/vocals-demucs-overlay-endpoints.ts default backend port
        8706,  # tests/e2e/vite.lyrics-words.config.ts API port
        8711,  # tests/e2e/playwright.cloudsync-ui.config.ts CLOUDSYNC_UI_HUB_PORT
        8712,  # tests/e2e/playwright.cloudsync-ui.config.ts CLOUDSYNC_UI_SPOKE_PORT
        9408,  # tests/e2e/stems-e2e-endpoints.ts DEFAULT_FRONTEND_PORT
        9410,  # tests/e2e/midi-maps-e2e-endpoints.ts DEFAULT_FRONTEND_PORT
        9414,  # tests/e2e/playwright.play-analytics.config.ts backend port
        9428,  # tests/e2e/playwright.library-wheel.config.ts engine port
        9473,  # tests/e2e/playwright.desktop-setup.config.ts DEAD_ENGINE_PORT
    }
)

# World-readable so a process running as a different unix user can read it
# (issue #1613): a git-common-dir registry lives under a checkout's own home
# directory, which a different uid's self-hosted CI runner typically cannot
# even traverse into. /tmp is sticky (mode 1777) on every platform this repo
# targets, so any uid may create files there and every uid may read them.
#
# The path is PINNED rather than derived from tempfile.gettempdir(), which
# follows TMPDIR/TEMP/TMP: a claimant under a private TMPDIR would write its
# markers somewhere scripts/ci_reap_port_holders.sh never reads, and the
# cross-uid attribution would silently degrade to an unnamed holder. The
# reaper's own MDT_CI_PORT_OWNER_REGISTRY_DIR default is the same literal, and
# a test asserts the two agree.
PORT_OWNER_REGISTRY_DIR = Path("/tmp/music-dj-tools-port-owners")


class PortConfigError(ValueError):
    """The worktree port contract is missing, invalid, or unsafe."""


class WorktreeEnvironmentError(PortConfigError):
    """The loaded ``apps`` tree does not belong to the current worktree."""


class WorktreeUnavailableError(PortConfigError):
    """No Git worktree exists for this process (e.g. an installed, non-checkout build)."""


@dataclass(frozen=True)
class WebuiPorts:
    """Validated backend and frontend ports for one worktree."""

    backend: int
    frontend: int

    def __post_init__(self) -> None:
        _validate_port(BACKEND_ENV, self.backend)
        _validate_port(FRONTEND_ENV, self.frontend)
        if self.backend == self.frontend:
            raise PortConfigError("backend and frontend ports must be different")

    @property
    def api_proxy_target(self) -> str:
        return f"http://127.0.0.1:{self.backend}"


def _validate_port(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PortConfigError(f"{name} must be an integer")
    if value < MIN_PORT or value > MAX_PORT:
        raise PortConfigError(f"{name} must be between {MIN_PORT} and {MAX_PORT}, got {value}")
    return value


def _parse_port(name: str, raw_value: str | int | None) -> int:
    if raw_value is None or str(raw_value).strip() == "":
        raise PortConfigError(f"{name} is required")
    value_text = str(raw_value).strip()
    if not value_text.isascii() or not value_text.isdecimal():
        raise PortConfigError(f"{name} must be an integer, got {value_text!r}")
    return _validate_port(name, int(value_text))


def _read_dotenv(dotenv_path: Path) -> dict[str, str]:
    if not dotenv_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.removeprefix("export ").strip()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def _resolved_raw_value(
    name: str,
    environ: Mapping[str, str],
    dotenv_values: Mapping[str, str],
) -> str | None:
    if name in environ:
        return environ[name]
    return dotenv_values.get(name)


def resolve_ports(
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path = WEBUI_ENV_FILE,
) -> WebuiPorts:
    """Resolve both ports with process environment above root ``.env``."""
    effective_environ = os.environ if environ is None else environ
    dotenv_values = _read_dotenv(dotenv_path)
    return WebuiPorts(
        backend=_parse_port(
            BACKEND_ENV,
            _resolved_raw_value(BACKEND_ENV, effective_environ, dotenv_values),
        ),
        frontend=_parse_port(
            FRONTEND_ENV,
            _resolved_raw_value(FRONTEND_ENV, effective_environ, dotenv_values),
        ),
    )


def resolve_backend_port(
    explicit_port: int | str | None,
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path = WEBUI_ENV_FILE,
) -> int:
    """Resolve CLI > process environment > root ``.env`` for the daemon."""
    if explicit_port is not None:
        return _parse_port("--port", explicit_port)
    effective_environ = os.environ if environ is None else environ
    dotenv_values = _read_dotenv(dotenv_path)
    return _parse_port(
        BACKEND_ENV,
        _resolved_raw_value(BACKEND_ENV, effective_environ, dotenv_values),
    )


def resolve_frontend_port(
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path = WEBUI_ENV_FILE,
) -> int:
    """Resolve the frontend port from process environment or root ``.env``."""
    effective_environ = os.environ if environ is None else environ
    dotenv_values = _read_dotenv(dotenv_path)
    return _parse_port(
        FRONTEND_ENV,
        _resolved_raw_value(FRONTEND_ENV, effective_environ, dotenv_values),
    )


def _write_dotenv_ports(dotenv_path: Path, ports: WebuiPorts) -> None:
    dotenv_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = (
        dotenv_path.read_text(encoding="utf-8").splitlines() if dotenv_path.is_file() else []
    )
    replacements = {
        BACKEND_ENV: str(ports.backend),
        FRONTEND_ENV: str(ports.frontend),
    }
    seen: set[str] = set()
    output_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        name = stripped.split("=", 1)[0].removeprefix("export ").strip()
        if name == OBSOLETE_PROXY_ENV and "=" in stripped:
            continue
        if name in replacements and "=" in stripped:
            output_lines.append(f"{name}={replacements[name]}")
            seen.add(name)
        else:
            output_lines.append(line)
    for name, value in replacements.items():
        if name not in seen:
            output_lines.append(f"{name}={value}")
    content = "\n".join(output_lines).rstrip() + "\n"
    # Skip the write when nothing changes: vite watches the root .env and
    # re-runs claim on every config load, so an unconditional rewrite feeds
    # its own change event and wedges the dev server in a restart loop.
    if dotenv_path.is_file() and dotenv_path.read_text(encoding="utf-8") == content:
        return
    _atomic_write_text(dotenv_path, content)


def _atomic_write_text(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temp_file:
        temp_file.write(content)
        temp_path = Path(temp_file.name)
    try:
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _port_owner_marker_path(port: int) -> Path | None:
    # Keyed by uid as well as port: /tmp is sticky, so only a file's owner may
    # rename or delete it there. Keying this way means every uid always writes
    # and removes only its OWN marker and never races another uid's, at the
    # cost of a stale marker (a crashed claimant's) surviving until something
    # reads and discounts it -- which is fine, since a marker is a lead for a
    # human to follow, never a live fact to signal on.
    #
    # The whole scheme attributes a cross-uid collision on a Linux CI host
    # (issue #1613); there is no such collision to attribute on Windows,
    # which has no os.getuid, so the marker is simply not written there.
    getuid = getattr(os, "getuid", None)
    return None if getuid is None else PORT_OWNER_REGISTRY_DIR / f"{port}-{getuid()}.owner"


def _write_port_owner_marker(port: int, worktree: str) -> None:
    """Record this worktree as the dynamic claimant of ``port`` (issue #1613)."""
    marker_path = _port_owner_marker_path(port)
    if marker_path is None:
        return
    PORT_OWNER_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PORT_OWNER_REGISTRY_DIR, 0o1777)
    except PermissionError:
        # A different uid created this directory first, which is the normal
        # shared-host case issue #1613 exists for: mode 1777 is already
        # sticky and world-writable, chmod is not this uid's to do, and
        # refusing to claim over a directory we merely cannot chmod would
        # defeat the whole feature. Re-raise if its mode is not actually
        # adequate -- then this is a real permission problem, not that case.
        if stat.S_IMODE(PORT_OWNER_REGISTRY_DIR.stat().st_mode) != 0o1777:
            raise
    content = (
        f"worktree={worktree}\n"
        f"claimed_at={datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"pid={os.getpid()}\n"
    )
    _atomic_write_text(marker_path, content)
    os.chmod(marker_path, 0o644)


def _remove_port_owner_marker(port: int) -> None:
    marker_path = _port_owner_marker_path(port)
    if marker_path is not None:
        marker_path.unlink(missing_ok=True)


def describe_port_owner(port: int) -> str | None:
    """Best-effort description of the last dynamic claimant of ``port``.

    Read is cross-uid by design (issue #1613): a self-hosted CI runner on the
    same host as this fleet's worktrees runs as a different unix user, so its
    ``/proc`` cannot see this worktree's process at all. A stale marker (the
    claimant's process is long gone) is still returned -- it names a worktree
    to go check, not a live fact -- so callers must not treat a hit here as
    proof the port is currently held by that worktree.
    """
    if not PORT_OWNER_REGISTRY_DIR.is_dir():
        return None
    candidates = sorted(
        PORT_OWNER_REGISTRY_DIR.glob(f"{port}-*.owner"),
        key=lambda candidate_path: candidate_path.stat().st_mtime,
        reverse=True,
    )
    for candidate_path in candidates:
        try:
            lines = candidate_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        fields = dict(line.split("=", 1) for line in lines if "=" in line)
        worktree = fields.get("worktree")
        if worktree:
            return f"worktree {worktree} (claimed_at={fields.get('claimed_at', 'unknown')})"
    return None


def _git_path(argument: str) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", argument],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def _repo_root(repo_root: Path | None) -> Path:
    return (repo_root or _git_path("--show-toplevel")).resolve()


def assert_source_tree_matches_worktree(
    *,
    source_root: Path = PROJECT_ROOT,
    repo_root: Path | None = None,
) -> Path | None:
    """Fail when the running ``apps`` tree belongs to a different worktree.

    ``uv run --no-sync`` resolves its environment from the nearest ANCESTOR
    ``pyproject.toml``. A directory without one of its own, a worktree checked
    out beneath the primary clone or any plain subdirectory of it, therefore
    binds the ancestor project's ``.venv``. That environment's editable install
    pins ``apps`` to the ancestor tree through a ``sys.meta_path`` finder, which
    outranks the working directory on ``sys.path``. The result is silent: this
    module's ``PROJECT_ROOT`` becomes the other worktree, so ``resolve_ports``
    reads that worktree's reserved ports while ``claim_ports`` writes this
    worktree's ``.env``, and two agents that believe they are isolated bind the
    same pair.

    Returns the current worktree root, or ``None`` when the working directory
    lies outside any Git worktree, where no worktree port contract exists to
    violate.
    """
    try:
        resolved_root = _repo_root(repo_root)
    except (subprocess.CalledProcessError, OSError):
        return None
    resolved_source = source_root.resolve()
    if resolved_source == resolved_root:
        return resolved_root
    raise WorktreeEnvironmentError(
        f"apps/ was loaded from {resolved_source} but the working directory "
        f"belongs to worktree {resolved_root}. The interpreter at "
        f"{sys.prefix} is another worktree's environment, so ports would be "
        f"read from {resolved_source / '.env'} while a claim writes "
        f"{resolved_root / '.env'}. Give this worktree its own environment "
        f"(`uv sync` in {resolved_root}) and re-run from there."
    )


def _common_dir(common_dir: Path | None) -> Path:
    return (common_dir or _git_path("--git-common-dir")).resolve()


def _registry_path(common_dir: Path) -> Path:
    return common_dir / "music-dj-tools" / "worktree-ports.json"


@contextmanager
def _locked_registry(common_dir: Path) -> Iterator[Path]:
    registry_path = _registry_path(common_dir)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    lock_dir = registry_path.parent / ".worktree-ports.lock"
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError as exc:
            if time.monotonic() >= deadline:
                raise PortConfigError(
                    f"port registry stayed locked at {lock_dir} for {LOCK_TIMEOUT_SECONDS:.1f}s"
                ) from exc
            time.sleep(0.05)
    try:
        yield registry_path
    finally:
        lock_dir.rmdir()


def _load_registry(registry_path: Path) -> dict[str, WebuiPorts]:
    if not registry_path.is_file():
        return {}
    try:
        raw_registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise PortConfigError(f"invalid port registry at {registry_path}: {exc}") from exc
    if not isinstance(raw_registry, dict):
        raise PortConfigError(f"invalid port registry object at {registry_path}")
    if raw_registry.get("schema") != 1:
        raise PortConfigError(f"unsupported port registry schema at {registry_path}")
    raw_reservations = raw_registry.get("reservations")
    if not isinstance(raw_reservations, dict):
        raise PortConfigError(f"invalid reservations in {registry_path}")
    reservations: dict[str, WebuiPorts] = {}
    claimed_ports: dict[int, str] = {}
    for path, raw_ports in raw_reservations.items():
        if not isinstance(path, str) or not isinstance(raw_ports, dict):
            raise PortConfigError(f"invalid reservation entry in {registry_path}")
        ports = WebuiPorts(
            backend=_parse_port("registry backend", raw_ports.get("backend")),
            frontend=_parse_port("registry frontend", raw_ports.get("frontend")),
        )
        for port in (ports.backend, ports.frontend):
            existing_path = claimed_ports.get(port)
            if existing_path is not None:
                raise PortConfigError(
                    f"registry port {port} is claimed by both {existing_path} and {path}"
                )
            claimed_ports[port] = path
        reservations[path] = ports
    return reservations


def _write_registry(
    registry_path: Path,
    reservations: Mapping[str, WebuiPorts],
) -> None:
    payload = {
        "schema": 1,
        "reservations": {
            path: {"backend": ports.backend, "frontend": ports.frontend}
            for path, ports in sorted(reservations.items())
        },
    }
    _atomic_write_text(registry_path, json.dumps(payload, indent=2) + "\n")


def _prune_missing_worktrees(
    reservations: Mapping[str, WebuiPorts],
) -> dict[str, WebuiPorts]:
    return {path: ports for path, ports in reservations.items() if Path(path).is_dir()}


def _port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # A claim is only valid when a fresh listener can bind the socket.
        # SO_REUSEADDR must remain off: enabling it lets a probe accept a
        # port which an incompatible leaked server still owns.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _pair_is_available(ports: WebuiPorts) -> bool:
    """Bind-test both candidate sockets together with address reuse disabled."""
    try:
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as backend_probe,
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as frontend_probe,
        ):
            for probe, port in (
                (backend_probe, ports.backend),
                (frontend_probe, ports.frontend),
            ):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _backend_listener_matches_pair(ports: WebuiPorts) -> bool:
    """Return whether the occupied backend exposes this pair's diagnostics."""
    url = f"{ports.api_proxy_target}/api/v1/settings"
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
        return False
    items = {
        item.get("key"): item.get("value")
        for group in payload["groups"]
        if isinstance(group, dict) and isinstance(group.get("items"), list)
        for item in group["items"]
        if isinstance(item, dict)
    }
    expected_origin = f"http://127.0.0.1:{ports.frontend}"
    return (
        items.get("port") == ports.backend
        and isinstance(items.get("cors_allow_origins"), list)
        and expected_origin in items["cors_allow_origins"]
    )


def _pair_owner(
    ports: WebuiPorts,
    reservations: Mapping[str, WebuiPorts],
) -> str | None:
    for path, reserved_ports in reservations.items():
        if ports == reserved_ports:
            return path
    return None


def _pair_overlaps_reservation(
    ports: WebuiPorts,
    reservations: Mapping[str, WebuiPorts],
    *,
    excluding_path: str,
) -> bool:
    candidate_ports = {ports.backend, ports.frontend}
    return any(
        path != excluding_path and bool(candidate_ports & {reserved.backend, reserved.frontend})
        for path, reserved in reservations.items()
    )


def _configured_candidate(
    dotenv_path: Path,
    environ: Mapping[str, str],
) -> WebuiPorts | None:
    dotenv_values = _read_dotenv(dotenv_path)
    backend_raw = _resolved_raw_value(BACKEND_ENV, environ, dotenv_values)
    frontend_raw = _resolved_raw_value(FRONTEND_ENV, environ, dotenv_values)
    backend_missing = backend_raw is None or backend_raw.strip() == ""
    frontend_missing = frontend_raw is None or frontend_raw.strip() == ""
    if backend_missing and frontend_missing:
        return None
    if backend_missing or frontend_missing:
        missing_name = BACKEND_ENV if backend_missing else FRONTEND_ENV
        raise PortConfigError(f"{missing_name} is required when claiming a port pair")
    return WebuiPorts(
        backend=_parse_port(BACKEND_ENV, backend_raw),
        frontend=_parse_port(FRONTEND_ENV, frontend_raw),
    )


def _reserved_port_from_process_environment(environ: Mapping[str, str]) -> int | None:
    """Return a reserved port this call INHERITED from the process environment.

    An exported ``MUSIC_DJ_BACKEND_PORT``/``MUSIC_DJ_FRONTEND_PORT`` outranks
    the ``.env`` file ``claim_ports`` rewrites, so it cannot be discarded the
    way a stale file value can: the very next read by ``just webui-ports``, a
    ``check``, or a service launch resolves the same reserved port again and
    fails ownership validation. A process-environment value is explicit
    configuration, exactly like a CLI flag, so it is refused rather than
    silently reallocated into a file the caller's own environment overrides.
    """
    for name in (BACKEND_ENV, FRONTEND_ENV):
        raw_value = environ.get(name)
        if raw_value is None or str(raw_value).strip() == "":
            continue
        port = _parse_port(name, raw_value)
        if port in RESERVED_FIXED_PORTS:
            return port
    return None


def _lane_pool_starts(lane: int) -> tuple[int, int]:
    """Return this lane's (backend, frontend) pool start, shifted by the stride."""
    offset = lane * PORT_LANE_STRIDE
    return BACKEND_POOL_START + offset, FRONTEND_POOL_START + offset


def _pair_in_lane_window(ports: WebuiPorts, lane: int) -> bool:
    """Return whether ``ports`` lies inside ``lane``'s pool window.

    A remembered pair from another lane is another runner's pair: the
    registry under ``.git`` survives ``actions/checkout``'s ``clean: true`` on a
    self-hosted runner, so a claim made before the runner had a lane would
    otherwise be restored verbatim on every later job (agentbox-3 derived lane
    4 and still claimed the lane-0 base pair, Sat 5 Sep 2026 18:00 UTC).
    """
    backend_start, frontend_start = _lane_pool_starts(lane)
    return (
        backend_start <= ports.backend < backend_start + POOL_SIZE
        and frontend_start <= ports.frontend < frontend_start + POOL_SIZE
    )


def _port_lane(environ: Mapping[str, str]) -> int:
    """Resolve the CI runner lane, defaulting to 0 (byte-identical to no lane)."""
    raw_value = environ.get(PORT_LANE_ENV)
    if raw_value is None or raw_value.strip() == "":
        return 0
    value_text = raw_value.strip()
    if not value_text.isascii() or not value_text.isdecimal():
        raise PortConfigError(f"{PORT_LANE_ENV} must be a non-negative integer, got {value_text!r}")
    return int(value_text)


def _allocate_pair(reservations: Mapping[str, WebuiPorts], lane: int) -> WebuiPorts:
    # RESERVED_FIXED_PORTS is excluded in EVERY lane, not just lane 0: a fixed
    # suite port is a compile-time constant with no lane of its own, so it can
    # coincide with any lane's window and must never be handed out regardless
    # of which lane is asking (issue #1613).
    reserved_ports = {
        port for ports in reservations.values() for port in (ports.backend, ports.frontend)
    } | RESERVED_FIXED_PORTS
    backend_pool_start, frontend_pool_start = _lane_pool_starts(lane)
    for slot in range(POOL_SIZE):
        candidate = WebuiPorts(
            backend=backend_pool_start + slot,
            frontend=frontend_pool_start + slot,
        )
        if candidate.backend in reserved_ports or candidate.frontend in reserved_ports:
            continue
        if _pair_is_available(candidate):
            return candidate
    raise PortConfigError(
        f"no free worktree port pair remains in lane {lane}; release stale reservations first"
    )


def claim_ports(
    *,
    repo_root: Path | None = None,
    common_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    requested: WebuiPorts | None = None,
) -> WebuiPorts:
    """Claim or restore a unique pair and rewrite the worktree root ``.env``."""
    resolved_root = _repo_root(repo_root)
    resolved_common_dir = _common_dir(common_dir)
    effective_environ = os.environ if environ is None else environ
    dotenv_path = resolved_root / ".env"
    if requested is not None:
        for reserved_candidate in (requested.backend, requested.frontend):
            if reserved_candidate in RESERVED_FIXED_PORTS:
                raise PortConfigError(
                    f"port {reserved_candidate} is reserved for a fixed-port CI/desktop "
                    "suite (see RESERVED_FIXED_PORTS in apps/webui/port_config.py); "
                    "choose a different pair"
                )
    inherited_reserved = _reserved_port_from_process_environment(effective_environ)
    if inherited_reserved is not None:
        raise PortConfigError(
            f"port {inherited_reserved} is reserved for a fixed-port CI/desktop suite and "
            f"was inherited from the process environment; unset {BACKEND_ENV}/"
            f"{FRONTEND_ENV} before claiming, since an exported value outranks the .env "
            "this would rewrite"
        )
    configured = requested or _configured_candidate(dotenv_path, effective_environ)
    if configured is not None and (
        configured.backend in RESERVED_FIXED_PORTS or configured.frontend in RESERVED_FIXED_PORTS
    ):
        # An existing checkout's own .env can predate RESERVED_FIXED_PORTS
        # (issue #1613): that is stale configuration, not an explicit
        # request, so discard it and fall through to a fresh allocation
        # instead of hard-failing a worktree that only needs to restart.
        configured = None
    lane = _port_lane(effective_environ)
    root_key = str(resolved_root)

    with _locked_registry(resolved_common_dir) as registry_path:
        reservations = _prune_missing_worktrees(_load_registry(registry_path))
        previous = dict(reservations)
        current = reservations.get(root_key)
        if current is not None and not _pair_in_lane_window(current, lane):
            # Remembered under another lane (or before this runner had one):
            # not ours to restore. Fall through to allocation inside the lane.
            current = None
        if current is not None and (
            current.backend in RESERVED_FIXED_PORTS or current.frontend in RESERVED_FIXED_PORTS
        ):
            # Same staleness as configured above, but recorded in the shared
            # registry rather than this checkout's own .env (issue #1613).
            current = None
        if (
            current is not None
            and (configured is None or configured == current)
            and (_pair_is_available(current) or _backend_listener_matches_pair(current))
        ):
            selected = current
        elif (
            configured is not None
            and not _pair_overlaps_reservation(
                configured,
                reservations,
                excluding_path=root_key,
            )
            and (_pair_is_available(configured) or _backend_listener_matches_pair(configured))
        ):
            selected = configured
        else:
            reservations.pop(root_key, None)
            selected = _allocate_pair(reservations, lane)

        reservations[root_key] = selected
        _write_registry(registry_path, reservations)
        try:
            _write_dotenv_ports(dotenv_path, selected)
        except BaseException:
            _write_registry(registry_path, previous)
            raise
    _write_port_owner_marker(selected.backend, root_key)
    _write_port_owner_marker(selected.frontend, root_key)
    return selected


def show_ports(
    *,
    repo_root: Path | None = None,
    common_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> WebuiPorts:
    """Return this worktree's reserved pair, or raise PortConfigError.

    Same refusal as ``python -m apps.webui.port_config show``. Raises
    ``WorktreeUnavailableError`` (a ``PortConfigError``) when this process is
    not running inside a Git worktree at all -- an installed build has no
    ``.git`` to resolve a pair against, which is a legitimate, expected state,
    not a bug, so it is reported as a named refusal rather than left to
    surface as an unhandled ``git`` subprocess failure.
    """
    try:
        resolved_repo_root = _repo_root(repo_root)
        resolved_common_dir = _common_dir(common_dir)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise WorktreeUnavailableError(
            "this process is not running inside a Git worktree (e.g. an "
            "installed build), so no worktree port pair can be resolved"
        ) from exc
    return _require_reservation(
        resolved_repo_root,
        resolved_common_dir,
        os.environ if environ is None else environ,
    )


def _require_reservation(
    repo_root: Path,
    common_dir: Path,
    environ: Mapping[str, str],
) -> WebuiPorts:
    ports = resolve_ports(environ=environ, dotenv_path=repo_root / ".env")
    with _locked_registry(common_dir) as registry_path:
        reservations = _prune_missing_worktrees(_load_registry(registry_path))
        owner = _pair_owner(ports, reservations)
        if reservations.get(str(repo_root)) != ports:
            owner_note = f"; currently owned by {owner}" if owner is not None else ""
            raise PortConfigError(
                f"port pair {ports.backend}/{ports.frontend} is not reserved by "
                f"this worktree{owner_note}; run `just webui-ports-claim`"
            )
    return ports


def check_reservation(
    service: str,
    *,
    repo_root: Path | None = None,
    common_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> WebuiPorts:
    """Verify registry ownership and availability for one service or both."""
    if service not in {"backend", "frontend", "all"}:
        raise PortConfigError(f"unknown service {service!r}")
    resolved_root = _repo_root(repo_root)
    resolved_common_dir = _common_dir(common_dir)
    effective_environ = os.environ if environ is None else environ
    ports = _require_reservation(
        resolved_root,
        resolved_common_dir,
        effective_environ,
    )
    checks = [("backend", ports.backend), ("frontend", ports.frontend)]
    if service != "all":
        checks = [(service, getattr(ports, service))]
    for service_name, port in checks:
        if not _port_is_available(port):
            raise PortConfigError(f"{service_name} port {port} is already in use")
    if (
        service == "frontend"
        and not _port_is_available(ports.backend)
        and not _backend_listener_matches_pair(ports)
    ):
        raise PortConfigError(
            f"backend port {ports.backend} is occupied by a process that does not "
            "match this worktree's reserved pair"
        )
    return ports


def release_ports(
    *,
    repo_root: Path | None = None,
    common_dir: Path | None = None,
) -> bool:
    """Release the current worktree's pair without deleting its ``.env``."""
    resolved_root = _repo_root(repo_root)
    resolved_common_dir = _common_dir(common_dir)
    with _locked_registry(resolved_common_dir) as registry_path:
        reservations = _prune_missing_worktrees(_load_registry(registry_path))
        released_ports = reservations.pop(str(resolved_root), None)
        _write_registry(registry_path, reservations)
    if released_ports is not None:
        _remove_port_owner_marker(released_ports.backend)
        _remove_port_owner_marker(released_ports.frontend)
    return released_ports is not None


def _print_ports(ports: WebuiPorts) -> None:
    print(f"backend  http://127.0.0.1:{ports.backend}")
    print(f"frontend http://127.0.0.1:{ports.frontend}")
    print(f"proxy    {ports.api_proxy_target}")


def _print_ports_json(ports: WebuiPorts) -> None:
    print(
        json.dumps(
            {
                "backend": ports.backend,
                "frontend": ports.frontend,
                "api_proxy_target": ports.api_proxy_target,
            }
        )
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.webui.port_config",
        description="claim and validate worktree-local Web UI ports",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    claim_parser = commands.add_parser("claim", help="claim or restore a unique pair")
    claim_parser.add_argument("--backend-port", type=int)
    claim_parser.add_argument("--frontend-port", type=int)
    claim_parser.add_argument("--json", action="store_true")
    check_parser = commands.add_parser("check", help="check ownership and availability")
    check_parser.add_argument(
        "--service",
        choices=("backend", "frontend", "all"),
        default="all",
    )
    check_parser.add_argument("--json", action="store_true")
    show_parser = commands.add_parser("show", help="show the reserved pair")
    show_parser.add_argument("--json", action="store_true")
    commands.add_parser("release", help="release the current worktree's pair")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        assert_source_tree_matches_worktree()
        if args.command == "claim":
            if (args.backend_port is None) != (args.frontend_port is None):
                parser.error("--backend-port and --frontend-port must be supplied together")
            requested = (
                WebuiPorts(args.backend_port, args.frontend_port)
                if args.backend_port is not None
                else None
            )
            ports = claim_ports(requested=requested)
            if args.json:
                _print_ports_json(ports)
            else:
                print("[OK] Reserved worktree ports")
                _print_ports(ports)
        elif args.command == "check":
            ports = check_reservation(args.service)
            if args.json:
                _print_ports_json(ports)
            else:
                print(f"[OK] {args.service} port reservation is free")
                _print_ports(ports)
        elif args.command == "show":
            ports = show_ports()
            if args.json:
                _print_ports_json(ports)
            else:
                _print_ports(ports)
        elif args.command == "release":
            released = release_ports()
            print("[OK] Released worktree ports" if released else "[OK] No reservation")
        else:
            parser.error(f"unhandled command {args.command!r}")
    except PortConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PORT_LANE_ENV",
    "PORT_OWNER_REGISTRY_DIR",
    "RESERVED_FIXED_PORTS",
    "PortConfigError",
    "WebuiPorts",
    "WorktreeUnavailableError",
    "check_reservation",
    "claim_ports",
    "describe_port_owner",
    "main",
    "release_ports",
    "resolve_backend_port",
    "resolve_frontend_port",
    "resolve_ports",
    "show_ports",
]
