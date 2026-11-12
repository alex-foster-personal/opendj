"""perf-lab stacks: run one worktree's own engine + vite on its own ports.

A stack is the dev-server pair the driver and abwin.mjs profile. Each one:

  - runs from a git worktree on THAT worktree's own installed dependencies:
    every `up` runs the worktree's locked installs (`pnpm install
    --frozen-lockfile` in apps/webui/frontend, from pnpm's shared store, and
    `uv sync --locked --extra dev` at the worktree root), even when
    node_modules and .venv exist: a checkout moved to a new HEAD keeps the old
    revision's dependencies, and both installers are quick no-ops when the
    environment already matches the lockfile. Nothing is borrowed from
    another checkout;
  - claims its ports through the repo's port registry (apps.webui.port_config
    claim, run by the worktree's own interpreter, which writes its .env);
  - runs the engine with PYTHONPATH = the worktree against its OWN copy of the
    fixture data dir, so two stacks never share one state.db;
  - records one manifest (manifest.py: commit, dirty diff, lockfiles, browser
    build, interpreter, node, fixture digest, host) in its state, which every
    capture against it copies and compare checks for equality. A capture
    re-measures the commit, dirty diff and lockfiles (manifest.py drift) when
    it starts and ends, so a worktree edited after `up` is never attributed to
    the old manifest.
  - runs only a clean tree, so its `head` is the code that serves: `up`
    refuses a worktree with tracked changes, or with a file outside git's
    index where a stack process resolves code (executed_roots: every
    PYTHONPATH entry stack_env sets and the engine's working directory, which
    is the worktree root, so a root sitecustomize.py counts; vite's root; and
    the root .env files). An untracked file counts unless it is a
    NON_CODE_SUFFIXES document outside vite's root. A gitignored one counts
    when a process can still load it (counted_untracked): a sourceless
    sitecustomize.pyc, a built extension module such as the root
    _rb_waveform_native*.so, an ignored .js under vite's root, a .env.local,
    or a root .env holding more than the ports `up` writes. node_modules is
    governed by the frozen install instead, and a __pycache__ pyc Python
    validates against its source does not count. `--allow-dirty` runs one
    anyway for development, and then `head` is `<sha>+dirty.<hash>` (a hash of
    `git diff HEAD --binary` and of every counted file outside the index), never
    the bare SHA, which ledger.py refuses as a commit. `verify` (and `show`)
    re-derive that identity and exit 3 when HEAD or the tree changed since
    `up`; `verify` also exits 3 when a launched process died or no longer
    holds its port (another server took it): run `verify` after a capture,
    before it is ledgered.

Usage (from a checkout with psutil, e.g. `uv run --locked python ...`):
  python scripts/perf/lab/stack.py up   --name base --worktree ../music-dj-tools-wt-base --data-src DATA
  python scripts/perf/lab/stack.py show --name base
  python scripts/perf/lab/stack.py verify --name base
  python scripts/perf/lab/stack.py down --name base
State: <scratch>/stacks/<name>.json (ports, pids, data copy, browser,
manifest), logs beside
it. <scratch> is $PERFLAB_SCRATCH, or .tmp/perf-lab in this checkout.
Exit codes: 0 ok, 1 started but never healthy (torn down) or survivors on
down, 2 refused before starting anything, 3 show of a stack that no longer
answers, or show/verify of a stack whose tree changed since up (UNKNOWN).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.machinery
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# this file runs as a script: the harness root first, so its own scripts package wins
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.perf.lab import manifest as stack_manifest

HARNESS = Path(__file__).resolve().parents[3]

from scripts.perf.lab.envs import base_env, git_env, install_env, tool_env  # noqa: E402


def _scratch() -> Path:
    root = Path(os.environ.get("PERFLAB_SCRATCH", HARNESS / ".tmp" / "perf-lab"))
    (root / "stacks").mkdir(parents=True, exist_ok=True)
    return root


def _state_file(name: str) -> Path:
    return _scratch() / "stacks" / f"{name}.json"


def _claim_file(name: str) -> Path:
    """The capture's exclusive claim on a stack (driver.mjs creates it O_EXCL)."""
    return _scratch() / "stacks" / f"{name}.json.claim"


def _exe(name: str) -> str:
    """An absolute executable from PATH; a relative entry would name another
    file per working directory. Raises FileNotFoundError when there is none."""
    found = shutil.which(name)
    if found is None or not Path(found).is_absolute():
        raise FileNotFoundError(f"{name} is not on PATH as an absolute executable (found {found!r})")
    return found


def venv_python(wt: Path, windows: bool = os.name == "nt") -> Path:
    """The worktree's own interpreter: Scripts/python.exe on Windows, bin/python elsewhere."""
    return wt / ".venv" / ("Scripts/python.exe" if windows else "bin/python")


def locked_installs(wt: Path) -> list[tuple[list[str], Path]]:
    """The installs `up` runs before anything starts, as (argv, cwd): both,
    every time. An existing node_modules or .venv proves only that some
    revision was installed, not this one, so presence is never a reason to
    skip; each installer brings the environment to the worktree's own
    lockfile (uv sync is exact, so it also removes what the lock lacks) and
    refuses a lockfile out of date with its manifest."""
    fe = wt / "apps/webui/frontend"
    return [(["pnpm", "install", "--frozen-lockfile"], fe), (["uv", "sync", "--locked", "--extra", "dev"], wt)]


def install_deps(wt: Path, log: Path) -> None:
    """Run locked_installs(wt), appending their output to log. Raises
    CalledProcessError or FileNotFoundError when an install cannot finish."""
    # the lockfile alone decides what is installed: no UV_*, npm_config_*, PNPM_*,
    # NODE_OPTIONS or VIRTUAL_ENV (envs.install_env)
    env = install_env(os.environ)
    with open(log, "a", encoding="utf-8", newline="\n") as fh:
        for argv, cwd in locked_installs(wt):
            fh.write(f"$ {' '.join(argv)}  (in {cwd})\n")
            fh.flush()
            subprocess.run(
                [_exe(argv[0]), *argv[1:]], cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT, check=True
            )


def _claim_ports(wt: Path) -> tuple[int, int]:
    out = subprocess.run(
        [str(venv_python(wt)), "-m", "apps.webui.port_config", "claim", "--json"],
        cwd=wt,
        # MUSIC_DJ_* port variables would override the claim (envs.tool_env)
        env=tool_env(os.environ, PYTHONPATH=str(wt)),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    p = json.loads(out)
    return int(p["backend"]), int(p["frontend"])


def _wait_http(url: str, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while True:
        with contextlib.suppress(Exception), urllib.request.urlopen(url, timeout=3) as r:
            if r.status == 200:
                return True
        if time.monotonic() >= end:
            return False
        time.sleep(1.0)


def _listening(port: int) -> bool:
    """Whether anything accepts a TCP connection on 127.0.0.1:port."""
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
        return True
    return False


def _owns_listener(pid: int | None, port: int) -> str | None:
    """None when process pid (or one of its descendants) is alive and is the
    one listening on port, else why not. A health probe alone cannot tell the
    stack's server from an older one already bound there."""
    import psutil

    if pid is None:
        return "was not launched"
    try:
        root = psutil.Process(pid)
        if not root.is_running() or root.status() == psutil.STATUS_ZOMBIE:
            return f"pid {pid} exited"
        for proc in (root, *root.children(recursive=True)):
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                for c in proc.net_connections(kind="inet"):
                    if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port:
                        return None
    except psutil.NoSuchProcess:
        return f"pid {pid} exited"
    except psutil.AccessDenied as e:
        return f"cannot read pid {pid}'s sockets: {e}"
    return f"pid {pid} is not the process listening on port {port}"


def ownership_faults(state: Mapping[str, Any]) -> list[str]:
    """Why the stack in state is not the one serving its ports: each launched
    process must still be the one it started (same create time, so a reused pid
    does not pass) and must, itself or a descendant, hold its port's listener."""
    faults = []
    for role, key, port_key in (("engine", "engine_pid", "backend"), ("vite", "vite_pid", "frontend")):
        pid = state.get(key)
        recorded = state.get("create_times", {}).get(str(pid))
        if pid is not None and (recorded is None or _create_time(pid) != recorded):
            faults.append(f"{role}: pid {pid} is not the process this stack launched")
        elif why := _owns_listener(pid, state[port_key]):
            faults.append(f"{role}: {why}")
    return faults


def materialize_fixture(src: Path, data: Path) -> None:
    """Copy the fixture at src into data as real files. Links are followed: a
    copied link would let two stacks' engines write one state.db, or write
    through to the canonical fixture. A failed copy is removed."""
    if not src.is_dir():
        raise FileNotFoundError(f"fixture data dir {src} does not exist")
    if data.exists():
        shutil.rmtree(data)
    try:
        shutil.copytree(src, data, symlinks=False)
    except BaseException:
        shutil.rmtree(data, ignore_errors=True)
        raise


# The engine and vite get the operator's environment only by allowlist
# (envs.base_env): what a process needs to find its programs, home and temp
# space, nothing else. A denylist could never be complete: PYTHONMALLOC,
# PYTHONDEVMODE, MALLOC_ARENA_MAX, GLIBC_TUNABLES, LD_PRELOAD, NODE_OPTIONS or
# UV_THREADPOOL_SIZE change engine CPU and memory, and the app's own
# MUSIC_DJ_*, VITE_*, MDT_*, ODJ_* switches change its workload, all without
# reaching the manifest. Locale and time zone are left out too, so each
# platform's default applies alike to every stack. Every other launch here
# takes its own builder from envs.py.


def stack_env(wt: Path, data: Path, inherited: Mapping[str, str]) -> dict[str, str]:
    """The engine's and vite's environment: the allowlisted part of the
    operator's (envs.base_env), with this stack's worktree, data copy and
    library mode."""
    env = base_env(inherited)
    env.update(
        {
            "PYTHONPATH": str(wt),
            "MDT_DATA_DIR": str(data),
            # normalized as the app reads it (platform_paths), so the manifest records the effective mode
            "MDT_LIBRARY_MODE": inherited.get("MDT_LIBRARY_MODE", "").strip().lower() or "local",
            "VITE_API_BASE": "",  # same origin, through this stack's own vite proxy
            # a capture measures the app, never MusicBrainz or Cover Art Archive:
            # a track without local artwork answers from the fixture alone
            "ODJ_ARTWORK_ONLINE": "0",
        }
    )
    # a fixture pack in remote mode serves ANLZ from $MDT_CRATE_ROOT/pioneer-share
    if env["MDT_LIBRARY_MODE"] == "remote":
        env["MDT_CRATE_ROOT"] = str(data)
    return env


def _vite_argv(fe: Path) -> list[str]:
    """Node plus vite's own JavaScript entry: node_modules/.bin/vite is a shell
    shim on POSIX and a .cmd on Windows, neither of which is launched by path."""
    pkg = fe / "node_modules/vite/package.json"
    bin_field = json.loads(pkg.read_text(encoding="utf-8"))["bin"]
    entry = bin_field if isinstance(bin_field, str) else bin_field["vite"]
    return [_node(fe), str(pkg.parent / entry)]


def _node(fe: Path) -> str:
    """A Node checked against the engines floor of the frontend it will run,
    fe/package.json (scripts.perf.node_runtime): an older Node on PATH can
    still start vite, and its numbers would then be published from a runtime
    outside that frontend's floor. The floor is the launched worktree's, never
    this harness checkout's, which can be another revision."""
    from scripts.perf.node_runtime import resolved_node

    try:
        return resolved_node(package_json=fe / "package.json")
    except SystemExit as e:  # its refusal, as an error cmd_up reports and survives
        raise ValueError(str(e)) from None


def _create_time(pid: int) -> float | None:
    import psutil

    try:
        return float(psutil.Process(pid).create_time())
    except psutil.Error:
        return None


def stop_tree(
    pids: list[int], create_times: Mapping[str, float | None], grace: float = 5.0
) -> tuple[list[int], list[int]]:
    """Terminate each pid and its descendants, then kill what outlives `grace`.
    A pid whose creation time is not the one recorded at `up` is someone else's
    now (recycled after a crash or reboot) and is left alone. Returns (skipped
    pids, pids still present after the kill); a survivor is never reported
    stopped. psutil walks the tree on every platform, so no process groups."""
    import psutil

    procs: list[psutil.Process] = []
    skipped: list[int] = []
    for pid in pids:
        recorded = create_times.get(str(pid))
        if recorded is None or _create_time(pid) != recorded:
            skipped.append(pid)
            continue
        with contextlib.suppress(psutil.Error):
            root = psutil.Process(pid)
            procs += [*root.children(recursive=True), root]
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.terminate()
    _gone, alive = psutil.wait_procs(procs, timeout=grace)
    for proc in alive:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    _gone, survivors = psutil.wait_procs(alive, timeout=grace)
    return skipped, sorted(p.pid for p in survivors)


def engine_cwd(wt: Path) -> Path:
    """The engine's working directory, which `python -m` puts first on sys.path."""
    return wt


def vite_root(wt: Path) -> Path:
    """vite's root: it serves and bundles any file under it, of any type."""
    return wt / "apps/webui/frontend"


# Documents and images, never imported or executed from a Python import root.
# Outside vite's root an untracked file of these types does not make a tree
# dirty; under it every untracked file does (vite serves public/ as is).
NON_CODE_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".log"})


def env_dir(wt: Path) -> Path:
    """Where the stack's .env files are read: vite.config.ts sets vite's envDir
    to the repository root, and the engine's port_config and library_mode read
    the root .env."""
    return wt


# The root .env lines `up` itself writes through the port registry (the
# ports are recorded in the stack's state). Every other line in it is
# configuration a developer set, and counts.
CLAIMED_ENV_KEYS = frozenset({"MUSIC_DJ_BACKEND_PORT", "MUSIC_DJ_FRONTEND_PORT", "MUSIC_DJ_API_PROXY_TARGET"})


def _env_settings(path: Path) -> bytes:
    """The root .env without the lines the port claim owns, comments or blanks."""
    kept = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        name = stripped.split("=", 1)[0].removeprefix("export ").strip()
        if stripped and not stripped.startswith("#") and name not in CLAIMED_ENV_KEYS:
            kept.append(stripped)
    return "\n".join(kept).encode()


def counted_content(wt: Path, rel: str) -> bytes:
    """The bytes of a counted file that the identity hashes."""
    if rel == (env_dir(wt).resolve() / ".env").relative_to(wt.resolve()).as_posix():
        return _env_settings(wt / rel)
    return (wt / rel).read_bytes()


def executed_roots(wt: Path) -> dict[str, list[Path]]:
    """Where a stack's processes resolve code, derived from how they are
    launched rather than listed: every PYTHONPATH entry stack_env sets plus the
    engine's working directory ("python"), vite's root ("vite"), where the
    .env files are read ("env"), and the worktree's own site-packages ("site"). A new PYTHONPATH entry is scanned the day
    stack_env sets it."""
    env = stack_env(wt, wt / ".perflab-data", {})
    python = [Path(p) for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    python.append(engine_cwd(wt))
    return {
        "python": list(dict.fromkeys(python)),
        "vite": [vite_root(wt)],
        "env": [env_dir(wt)],
        "site": site_roots(wt),
    }


def site_roots(wt: Path) -> list[Path]:
    """The worktree's own interpreter site-packages (its .venv). Python runs
    sitecustomize, usercustomize and every .pth in a site directory before the
    engine starts, so those files execute whether or not the engine imports
    them. A site directory outside wt is not vouched for by git, so only the
    worktree's own .venv counts here; the installed packages themselves are
    governed by the frozen lockfile install, and only the startup hooks below
    are counted from it (site_hook)."""
    found = sorted((wt / ".venv").glob("lib/python*/site-packages"))
    found += [d for d in [wt / ".venv/Lib/site-packages"] if d.is_dir()]
    return found


def site_hook(parts: tuple[str, ...]) -> bool:
    """Whether a file directly in a site directory is a startup hook: a
    sitecustomize or usercustomize module (source or bytecode) or any .pth
    file, which Python executes when it names an import line."""
    if len(parts) != 1:
        return False
    name = parts[0].lower()
    return name in {"sitecustomize.py", "sitecustomize.pyc", "usercustomize.py", "usercustomize.pyc"} or name.endswith(
        ".pth"
    )


# Gitignored files a stack process can still load. An ignore rule hides a file
# from `git status`, not from the import system or vite, so these count toward
# the tree identity exactly like untracked ones (thread 4285011673). The test
# is an allowlist of loadable kinds, not a denylist of harmless ones.
#
# Python: any importer or extension suffix this interpreter knows, plus every
# platform's extension and path-file suffixes, so a tree checked on one OS
# counts what another would load. A file is reachable only through a dotted
# name, so it counts only when every directory between its import root and it
# is a valid identifier (.venv, .mypy_cache and *.egg-info never are).
PY_LOADABLE_SUFFIXES = frozenset(
    {*importlib.machinery.all_suffixes(), ".py", ".pyw", ".pyc", ".so", ".pyd", ".dylib", ".pth"}
)
# vite: what its resolver, plugins and env loader read under its root.
VITE_LOADABLE_SUFFIXES = frozenset(
    {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx", ".svelte", ".vue", ".css", ".json", ".html"}
)
# node_modules is never descended into: the lockfile governs it, and every
# `up` runs the frozen install. Below vite's root these do not count, each for
# a stated reason:
#   .svelte-kit:  rewritten from tracked routes and config by the SvelteKit
#                 plugin's sync when the dev server starts.
#   build, dist:  outputs of `vite build` and the static adapter; the dev
#                 server's module graph starts at tracked sources and never
#                 resolves into them.
# (A Python import root still counts a loadable file in any of them.) A
# directory holding a CACHEDIR.TAG (cargo's target/, .ruff_cache) is a tool's
# own cache by the Cache Directory Tagging spec, never an import root; a module
# copied OUT of one into an import root (the root _rb_waveform_native*.so) does
# count. __pycache__ is not skipped: see _pycache_counts.
VITE_OUTPUT_DIRS = frozenset({".svelte-kit", "build", "dist"})


def _pycache_counts(path: Path) -> bool:
    """Whether a .pyc inside __pycache__ can run code its source does not hold.
    Python reads __pycache__ only for a source file that exists, and loads the
    cached code only after validating it against that source: a timestamp pyc
    (flags 0) by the source's mtime and size, a checked-hash pyc (flags 3) by
    its hash. An unchecked-hash pyc (flags 1) is loaded without looking at the
    source, so it counts, as does any pyc whose header cannot be read."""
    try:
        with path.open("rb") as f:
            header = f.read(8)
    except OSError:
        return True
    if len(header) < 8:
        return True
    return int.from_bytes(header[4:8], "little") & 0b11 == 0b01


def _python_loadable(rel_parts: tuple[str, ...], path: Path) -> bool:
    """rel_parts is the path below one Python import root."""
    *dirs, name = rel_parts
    if not any(name.lower().endswith(s) for s in PY_LOADABLE_SUFFIXES):
        return False
    if dirs and dirs[-1] == "__pycache__":
        return all(d.isidentifier() for d in dirs[:-1]) and _pycache_counts(path)
    return all(d.isidentifier() and d != "__pycache__" for d in dirs)


def _vite_loadable(name: str) -> bool:
    return name.startswith(".env") or Path(name).suffix.lower() in VITE_LOADABLE_SUFFIXES


def _ignored_files(wt: Path, specs: list[str]) -> list[str]:
    """Every gitignored file under specs, walking each wholly ignored directory
    git reports as one entry, minus node_modules and CACHEDIR.TAG caches."""
    entries = (
        _git(wt, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "--", *specs)
        .decode()
        .splitlines()
    )
    out: list[str] = []
    for entry in entries:
        if not entry.endswith("/"):
            out.append(entry)
            continue
        for top, dirs, files in os.walk(wt / entry):
            here = Path(top)
            if here.name == "node_modules" or (here / "CACHEDIR.TAG").exists():
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d != "node_modules"]
            out.extend((here / f).relative_to(wt).as_posix() for f in files)
    return out


def counted_untracked(wt: Path) -> list[str]:
    """The files outside git's index that a stack of wt could execute.
    Untracked, non-ignored: any under vite's root, and any but
    NON_CODE_SUFFIXES under a Python import root. Gitignored: those a process
    can still load (PY_LOADABLE_SUFFIXES under a Python import root,
    VITE_LOADABLE_SUFFIXES and .env* under vite's root outside
    VITE_OUTPUT_DIRS), never in node_modules or a tool cache; and every .env*
    file directly in env_dir, the root .env only while it holds a setting
    besides the ports `up` writes (CLAIMED_ENV_KEYS). A root outside wt cannot
    be vouched for by git, and is refused."""
    base = wt.resolve()
    rels: dict[str, list[str]] = {}
    for kind, roots in executed_roots(wt).items():
        for root in roots:
            try:
                rel = root.resolve().relative_to(base).as_posix()
            except ValueError:
                raise ValueError(f"{kind} root {root} lies outside {wt}, so git cannot vouch for it") from None
            rels.setdefault(kind, []).append(rel)

    def below(f: str, kind: str) -> list[tuple[str, ...]]:
        """f's parts below each `kind` root that holds it."""
        parts = tuple(f.split("/"))
        found = []
        for r in rels.get(kind, []):
            rp = () if r == "." else tuple(r.split("/"))
            if parts[: len(rp)] == rp and len(parts) > len(rp):
                found.append(parts[len(rp) :])
        return found

    dotenv = (env_dir(wt).resolve() / ".env").relative_to(base).as_posix()
    specs = sorted({r for v in rels.values() for r in v})
    files = _git(wt, "ls-files", "--others", "--exclude-standard", "--", *specs).decode().splitlines()
    code = {f for f in files if below(f, "python") and Path(f).suffix.lower() not in NON_CODE_SUFFIXES}
    code |= {f for f in files if below(f, "vite")}
    for f in _ignored_files(wt, specs):
        if "node_modules" in f.split("/"):
            continue
        if (
            any(_python_loadable(p, wt / f) for p in below(f, "python"))
            or any(_vite_loadable(p[-1]) and not VITE_OUTPUT_DIRS.intersection(p[:-1]) for p in below(f, "vite"))
            or any(len(p) == 1 and p[0].startswith(".env") for p in below(f, "env"))
            or any(site_hook(p) for p in below(f, "site"))
        ):
            code.add(f)
    return sorted(f for f in code if f != dotenv or _env_settings(wt / f))


def _git(wt: Path, *args: str) -> bytes:
    return subprocess.run(
        [_exe("git"), "-C", str(wt), *args], env=git_env(os.environ), capture_output=True, check=True
    ).stdout


def tree_identity(wt: Path) -> dict[str, Any]:
    """What a stack of wt runs: HEAD, plus, when the tree is dirty, the tracked
    changes and the untracked files a stack could execute (counted_untracked)
    with a content hash of both. `revision` is HEAD only for a clean tree; a dirty one reads
    `<sha>+dirty.<hash>`, so its numbers are never filed under the bare SHA."""
    head = _git(wt, "rev-parse", "HEAD").decode().strip()
    tracked = _git(wt, "status", "--porcelain", "--untracked-files=no").decode().splitlines()
    untracked = counted_untracked(wt)
    if not tracked and not untracked:
        return {"revision": head, "head": head, "dirty": False}
    h = hashlib.sha256(_git(wt, "diff", "HEAD", "--binary"))
    for rel in untracked:
        h.update(b"\0" + rel.encode() + b"\0")
        with contextlib.suppress(OSError):
            h.update(counted_content(wt, rel))
    content = h.hexdigest()
    return {
        "revision": f"{head}+dirty.{content[:16]}",
        "head": head,
        "dirty": True,
        "tracked": tracked,
        "untracked": untracked,
        "content": content,
    }


def tree_drift(state: Mapping[str, Any]) -> str | None:
    """Why the stack's worktree no longer holds the code `up` recorded, or None."""
    recorded = state.get("tree")
    if not recorded:
        return "the stack recorded no tree identity at up"
    try:
        now = tree_identity(Path(state["worktree"]))
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        return f"the tree cannot be read: {type(e).__name__}: {e}"
    if now["head"] != recorded["head"]:
        return f"HEAD moved from {recorded['head'][:12]} to {now['head'][:12]}"
    if now != recorded:
        changed = sorted(
            set(now.get("tracked", []) + now.get("untracked", []))
            ^ set(recorded.get("tracked", []) + recorded.get("untracked", []))
        )
        detail = f": {', '.join(changed[:5])}" if changed else " (same paths, different contents)"
        return f"the worktree changed since up{detail}"
    return None


def detached_kwargs(platform: str = sys.platform) -> dict[str, Any]:
    """The platform seam that starts a stack process in its own process group,
    so the terminal's Ctrl-C (or a closing shell's hangup) reaches `up`, which
    tears the stack down through stop_tree, and never the servers directly:
    a new session on POSIX, a new process group on Windows."""
    if platform == "win32":
        # 0x200 is CREATE_NEW_PROCESS_GROUP; the constant exists only on Windows
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def _write_state(state: dict[str, Any]) -> None:
    _state_file(state["name"]).write_text(json.dumps(state, indent=1), encoding="utf-8", newline="\n")


def _launch(state: dict[str, Any], wt: Path, env: dict[str, str], sdir: Path) -> None:
    """Start the engine, record it, then vite, recording each as it starts so
    a failure between them leaves the engine findable."""
    name = state["name"]
    with open(sdir / f"{name}-engine.log", "w", encoding="utf-8", newline="\n") as log:
        engine = subprocess.Popen(
            [
                str(venv_python(wt)),
                *("-m", "apps.engine_core", "serve", "--data-dir", state["data"]),
                *("--host", "127.0.0.1", "--port", str(state["backend"])),
            ],
            cwd=engine_cwd(wt),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            **detached_kwargs(),
        )
    state["engine_pid"] = engine.pid
    state["create_times"][str(engine.pid)] = _create_time(engine.pid)
    _write_state(state)
    fe = vite_root(wt)
    with open(sdir / f"{name}-vite.log", "w", encoding="utf-8", newline="\n") as log:
        vite = subprocess.Popen(
            [*_vite_argv(fe), "dev", "--host", "127.0.0.1", "--port", str(state["frontend"]), "--strictPort"],
            cwd=fe,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            **detached_kwargs(),
        )
    state["vite_pid"] = vite.pid
    state["create_times"][str(vite.pid)] = _create_time(vite.pid)
    _write_state(state)


def _teardown(state: dict[str, Any]) -> list[int]:
    """Stop what the stack started, then drop its data copy and state file;
    returns survivors, in which case both are kept for a later `down`."""
    started = [p for p in (state.get("vite_pid"), state.get("engine_pid")) if p is not None]
    _skipped, survivors = stop_tree(started, state.get("create_times", {}))
    if not survivors:
        shutil.rmtree(state["data"], ignore_errors=True)
        _state_file(state["name"]).unlink(missing_ok=True)
        _claim_file(state["name"]).unlink(missing_ok=True)
    return survivors


def _tree_to_run(wt: Path, allow_dirty: bool) -> tuple[dict[str, Any] | None, str | None]:
    """(identity, None) for a tree `up` may run, or (None, why not)."""
    try:
        tree = tree_identity(wt)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        return None, f"cannot read {wt} as a git tree: {e}"
    if tree["dirty"] and not allow_dirty:
        paths = tree["tracked"] + [f"?? {u}" for u in tree["untracked"]]
        return None, (
            f"{wt} is dirty, so HEAD is not the code that would run "
            f"({', '.join(paths[:5])}{' ...' if len(paths) > 5 else ''}); commit it, or pass --allow-dirty "
            "to record a content identity instead of the SHA"
        )
    return tree, None


def _require_unchanged(wt: Path, tree: Mapping[str, Any]) -> None:
    """Installs and the port claim must not change what runs."""
    if tree_identity(wt) != tree:
        raise ValueError(f"{wt} changed while its dependencies were installed and its ports claimed")


def cmd_up(a: argparse.Namespace) -> int:
    wt = Path(a.worktree).resolve()
    sdir = _scratch() / "stacks"
    if not (wt / "apps").is_dir():
        tree, refusal = None, f"{wt} is not a checkout"
    elif _state_file(a.name).exists():
        tree, refusal = None, f"stack {a.name} is already up; run `down --name {a.name}` first"
    else:
        tree, refusal = _tree_to_run(wt, a.allow_dirty)
    if tree is None:
        print(f"[FAIL] stack {a.name} not started: {refusal}", file=sys.stderr)
        return 2
    # no stack of this name is up, so a claim left by a crashed one is stale
    _claim_file(a.name).unlink(missing_ok=True)
    try:
        install_deps(wt, sdir / f"{a.name}-install.log")
        _vite_argv(vite_root(wt))  # a missing node or vite refuses before anything starts
        backend, frontend = _claim_ports(wt)
        # installs and the port claim must not have changed what runs
        _require_unchanged(wt, tree)
        data = sdir / f"data-{a.name}"
        materialize_fixture(Path(a.data_src).resolve(), data)
        # before the engine starts writing into the data copy
        # the node that runs vite and the capture, held to the launched worktree's floor
        node = _node(vite_root(wt))
        browser = stack_manifest.browser_identity(node, a.chrome)
        git = _exe("git")
        env = stack_env(wt, data, os.environ)
        manifest = stack_manifest.build(
            wt, data, venv_python(wt), node, git, browser, library_mode=env["MDT_LIBRARY_MODE"]
        )
    except (OSError, subprocess.CalledProcessError, KeyError, ValueError) as e:
        shutil.rmtree(sdir / f"data-{a.name}", ignore_errors=True)
        print(f"[FAIL] stack {a.name} not started: {type(e).__name__}: {e} (logs in {sdir})", file=sys.stderr)
        return 2
    state: dict[str, Any] = {
        "name": a.name,
        "worktree": str(wt),
        "head": tree["revision"],
        "tree": tree,
        "backend": backend,
        "frontend": frontend,
        "url": f"http://127.0.0.1:{frontend}",
        "engine_pid": None,
        "vite_pid": None,
        "create_times": {},
        "data": str(data),
        "data_src": str(Path(a.data_src).resolve()),
        "browser": browser["executable"],
        "python": str(venv_python(wt)),
        "git": git,
        "manifest": manifest,
    }
    # the worktree's claimed pair can already be serving (another stack, a dev
    # server): a probe would then pass against that server and its data
    busy = [p for p in (backend, frontend) if _listening(p)]
    if busy:
        shutil.rmtree(sdir / f"data-{a.name}", ignore_errors=True)
        print(
            f"[FAIL] stack {a.name} not started: port(s) {busy} already serving; stop that server first",
            file=sys.stderr,
        )
        return 2
    try:
        _launch(state, wt, env, sdir)
        healthy = (
            _wait_http(f"http://127.0.0.1:{backend}/api/v1/health", a.health_timeout)
            and _wait_http(f"{state['url']}/api/v1/health", a.health_timeout)
            and _wait_http(f"{state['url']}/performance", a.health_timeout)
        )
        # health answered; it must be this stack's own engine and vite answering
        owners = ownership_faults(state) if healthy else []
    except BaseException as e:
        survivors = _teardown(state)
        print(f"[FAIL] stack {a.name} could not start: {type(e).__name__}: {e}; survivors {survivors}", file=sys.stderr)
        if not isinstance(e, Exception):
            raise
        return 2
    if healthy and owners:
        survivors = _teardown(state)
        print(
            f"[FAIL] stack {a.name} answered but not from its own processes: {'; '.join(owners)} (survivors {survivors})",
            file=sys.stderr,
        )
        return 1
    if not healthy:
        survivors = _teardown(state)
        print(
            f"[FAIL] stack {a.name} not healthy within {a.health_timeout}s; stopped engine pid "
            f"{state['engine_pid']} and vite pid {state['vite_pid']} (survivors {survivors}); logs in {sdir}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(state))
    return 0


def cmd_down(a: argparse.Namespace) -> int:
    f = _state_file(a.name)
    if not f.exists():
        print(f"[FAIL] no stack {a.name}", file=sys.stderr)
        return 2
    survivors = _teardown(json.loads(f.read_text(encoding="utf-8")))
    if survivors:
        print(f"[FAIL] stack {a.name}: pids {survivors} survived the kill; state kept", file=sys.stderr)
        return 1
    print(f"[OK] stack {a.name} down")
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    f = a.state if a.state else _state_file(a.name)
    name = a.name or f.name.removesuffix(".json")
    a.name = name
    if not f.exists():
        print(f"[FAIL] no stack {name}", file=sys.stderr)
        return 2
    s = json.loads(f.read_text(encoding="utf-8"))
    # a state file outlives a crashed stack, so show proves it answers first
    if not (
        _wait_http(f"http://127.0.0.1:{s['backend']}/api/v1/health", 5) and _wait_http(f"{s['url']}/api/v1/health", 5)
    ):
        print(f"[FAIL] stack {a.name} is not answering; run `down --name {a.name}`", file=sys.stderr)
        return 3
    # a dead stack's ports can be taken by another server, which answers health
    # as well as this stack's own processes would
    owners = ownership_faults(s)
    if owners:
        print(
            f"[FAIL] stack {a.name} answers, but not from its own processes: {'; '.join(owners)}; "
            f"run `down --name {a.name}`",
            file=sys.stderr,
        )
        return 3
    drift = tree_drift(s)
    if drift:
        print(f"[FAIL] stack {a.name}: {drift}; its head no longer names the code serving", file=sys.stderr)
        return 3
    print(json.dumps(s, indent=1))
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    """Exit 0 when the stack still serves the code `up` recorded: its worktree
    unchanged (tree_drift) and each launched process alive, the one it started,
    and still the listener on its port (ownership_faults). 3 (UNKNOWN) when
    either fails: a capture taken meanwhile ran code its head does not name, or
    loaded a server that replaced a dead one on the same port."""
    f = _state_file(a.name)
    if not f.exists():
        print(f"[FAIL] no stack {a.name}", file=sys.stderr)
        return 2
    s = json.loads(f.read_text(encoding="utf-8"))
    faults = [d for d in (tree_drift(s),) if d] + ownership_faults(s)
    if faults:
        print(
            f"[UNKNOWN] stack {a.name}: {'; '.join(faults)}; what served since up is not {s['head']}", file=sys.stderr
        )
        return 3
    print(f"[OK] stack {a.name} still serves {s['head']} from the processes it launched")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up")
    u.add_argument("--name", required=True)
    u.add_argument("--worktree", required=True)
    u.add_argument("--data-src", required=True)
    u.add_argument("--chrome", help="the Chromium captures launch (default: the driver's own resolution)")
    u.add_argument("--health-timeout", type=float, default=240.0, help="seconds to wait for each health check")
    u.add_argument(
        "--allow-dirty",
        action="store_true",
        help="run a dirty worktree; head becomes <sha>+dirty.<content hash>, never the bare SHA",
    )
    sub.add_parser("down").add_argument("--name", required=True)
    show = sub.add_parser("show").add_mutually_exclusive_group(required=True)
    show.add_argument("--name")
    # the state file itself (driver.mjs --stack), wherever it lives
    show.add_argument("--state", type=Path)
    sub.add_parser("verify").add_argument("--name", required=True)
    a = ap.parse_args()
    return {"up": cmd_up, "down": cmd_down, "show": cmd_show, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
