"""perf-lab stack manifest: what a capture ran on, and whether two captures compare.

`stack.py up` builds one manifest per stack and keeps it in the stack's state
file (the `manifest` key of <scratch>/stacks/<name>.json). Every capture copies
the manifest of the stack it ran against: driver.json `manifest`, ab.py's
report.json `manifests`, and each ledger row `manifest`. Comparability is then
one equality check here, and no other file re-derives these fields.

A manifest is a flat object; every key in KEYS must be present and measured
(not null). A capture without one, or with one missing a key, is UNKNOWN for
comparison, never a pass. A host field no source on the host can read (a CPU
the OS does not name, say) is recorded as "UNKNOWN: ..." instead, so the stack
still comes up and captures there, and every comparison involving it is UNKNOWN.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

KEYS = (
    "commit",  # HEAD of the stack's worktree
    "dirty",  # False when clean, else sha256 of `git diff HEAD --binary` plus untracked files
    "pnpm_lock",  # sha256 of apps/webui/frontend/pnpm-lock.yaml
    "uv_lock",  # sha256 of uv.lock
    "browser_version",  # Chromium's own version string
    "browser_sha256",  # sha256 of the Chromium executable
    "python_version",  # the stack interpreter's sys.version
    "python_sha256",  # sha256 of the bytes that run the engine: the real interpreter executable and its libpython
    "node_version",
    "node_sha256",  # sha256 of the real node executable that runs vite and the capture
    "fixture",  # one hash over the materialized fixture data dir's names and contents
    "library_mode",  # the stack's effective MDT_LIBRARY_MODE: remote serves analysis and stems from the crate root
    "host_system",
    "host_release",  # the OS release and build: a kernel or macOS update changes scheduling, /proc and audio timing
    "host_machine",
    "host_cpus",
    "host_cpu_model",  # the processor's own model name: same OS, arch and core count is not the same machine class
    "pinned_content",  # the venv's own files that no RECORD vouches for, by content (pinned_content)
    "exempt_content",  # the exempt (built or patched) packages' installed bytes, by content (exempt_content)
)

# The code under test. A/B and the ledger exist to measure a change of code or
# dependencies, so these keys may differ between two compared captures; every
# other key is the environment and must be equal. This is the only allow-list.
CODE_UNDER_TEST = frozenset({"commit", "dirty", "pnpm_lock", "uv_lock", "exempt_content"})
# Of those, the dependency keys: a difference is reported as "deps differ".
DEPS = ("pnpm_lock", "uv_lock", "exempt_content")


def os_release() -> str:
    """The OS release and build, stable across reboots: macOS's product version
    plus Darwin's, Linux's kernel release plus its build string, Windows' build."""
    system = platform.system()
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]} (Darwin {platform.release()})"
    return f"{system} {platform.release()} {platform.version()}"


# A host field no source on this host could measure is recorded as this prefix
# plus why, so `stack.py up` still runs and captures there, while problem()
# keeps every comparison involving it UNKNOWN: two unmeasured hosts are never
# taken for one.
UNKNOWN = "UNKNOWN: "


def host_fields(measured: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The host_* manifest keys, each measured on this host (or taken from
    measured, the same keys as read elsewhere) or recorded UNKNOWN with why."""
    if measured is None:
        measured = {
            "host_system": platform.system(),
            "host_release": os_release(),
            "host_machine": platform.machine(),
            "host_cpus": os.cpu_count(),
            "host_cpu_model": cpu_model(),
        }
    return {
        k: v if v not in (None, "") else f"{UNKNOWN}{k} could not be read on this host" for k, v in measured.items()
    }


def unmeasured(m: Mapping[str, Any]) -> list[str]:
    """The keys of m recorded UNKNOWN."""
    return [k for k in KEYS if isinstance(m.get(k), str) and m[k].startswith(UNKNOWN)]


def problem(m: Any) -> str | None:
    """Why m is not a usable manifest for a comparison, or None."""
    if not isinstance(m, Mapping):
        return "no stack manifest (capture not run against a `stack.py up` stack)"
    missing = [k for k in KEYS if m.get(k) is None]
    if missing:
        return f"stack manifest lacks {', '.join(missing)}"
    unknown = unmeasured(m)
    return f"stack manifest could not measure {', '.join(unknown)} on its host" if unknown else None


def comparable(m: Any) -> Any:
    """m with only KEYS: what a ledger row keeps. The runtime trees (absolute
    paths on the machine that ran the stack) stay in the stack state and the
    capture's own files."""
    return {k: m[k] for k in KEYS if k in m} if isinstance(m, Mapping) else m


def differing(a: Mapping[str, Any], b: Mapping[str, Any], may_differ: frozenset[str] = frozenset()) -> list[str]:
    """The keys whose values differ between two usable manifests, outside may_differ."""
    return [k for k in KEYS if k not in may_differ and a[k] != b[k]]


def verdict(a: Any, b: Any, may_differ: frozenset[str] = CODE_UNDER_TEST) -> tuple[str | None, list[str]]:
    """(why the two captures cannot be compared, or None; the dependency keys
    that differ). The one comparability check every caller uses."""
    for side, m in (("first", a), ("second", b)):
        why = problem(m)
        if why:
            return f"{side} capture: {why}", []
    keys = differing(a, b, may_differ)
    if keys:
        return f"stack manifests differ in {', '.join(keys)}", []
    # exempt bytes are a dependency's identity: they may differ only when the
    # two stacks' pnpm lockfiles differ (a different lock builds different packages)
    if a["exempt_content"] != b["exempt_content"] and a["pnpm_lock"] == b["pnpm_lock"]:
        return (
            "stack manifests differ in exempt_content with the same pnpm lock (one lock builds the same packages)",
            [],
        )
    return None, [k for k in DEPS if a[k] != b[k]]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_digest(root: Path) -> str:
    """One deterministic hash over every file under root: its relative POSIX
    name and its contents' sha256, in sorted name order."""
    h = hashlib.sha256()
    files = sorted((p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file())
    for rel, p in files:
        h.update(rel.encode() + b"\0" + sha256_file(p).encode() + b"\n")
    return h.hexdigest()


def harness_digest(lab: Path = HERE) -> str:
    """One hash over the measurement harness: every top-level .py, .mjs and
    .lock (the scripts' own PEP 723 lockfiles, which pin their transitive
    dependencies) in the lab directory, by name and contents' sha256, in
    sorted name order.
    driver.mjs (harnessDigest), sampler.py and analyze.py each stamp it, so a
    capture made or analyzed by other measurement code never compares."""
    h = hashlib.sha256()
    for p in sorted(lab.iterdir(), key=lambda q: q.name):
        if p.is_file() and p.suffix in (".py", ".mjs", ".lock"):
            h.update(p.name.encode() + b"\0" + sha256_file(p).encode() + b"\n")
    return h.hexdigest()


def site_tree_sha256() -> str:
    """sha256 over the whole content of every site-packages directory of the
    running interpreter (purelib, platlib, the user site when enabled), by path
    relative to sys.prefix, plus the sitecustomize and usercustomize modules the
    interpreter imports at startup wherever they are. Unlike dist_content_digest
    (which reads only the files a distribution's RECORD lists and their
    immediate directories), this sees an unowned module, a .pth, a file in a
    directory no distribution lists, or a new subdirectory, all of which load
    and all of which change the digest. __pycache__ is skipped (bytecode the
    interpreter writes on import)."""
    import importlib.util
    import site
    import sysconfig

    prefix = Path(sys.prefix)
    dirs = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"], *site.getsitepackages()}
    if site.ENABLE_USER_SITE:
        dirs.add(site.getusersitepackages())
    h = hashlib.sha256()
    for d in sorted(Path(x).resolve() for x in dirs):
        h.update(
            f"dir {d.relative_to(prefix.resolve()) if d.is_relative_to(prefix.resolve()) else d}\0{tree_fingerprint(d, content=True)}\n".encode()
        )
    for mod in ("sitecustomize", "usercustomize"):
        spec = importlib.util.find_spec(mod)
        origin = spec.origin if spec is not None else None
        if origin is None:
            h.update(f"{mod}\0absent\n".encode())
        else:
            h.update(f"{mod}\0{sha256_file(Path(origin))}\n".encode())
    return h.hexdigest()


def measurement_deps(names: tuple[str, ...]) -> dict[str, str]:
    """The realized versions of every distribution installed in the
    environment running a measurement script, and the installed bytes
    (content_sha256, dist_content_digest) of each named one and what it
    requires, since a patched install keeps its version (its transitive dependencies
    too: a protobuf under an unchanged perfetto can change parsing), plus its
    interpreter and each named dependency. sampler.py and
    analyze.py run in their own PEP 723 environments (locked by
    <script>.py.lock), so the source digest alone cannot say which psutil or
    perfetto measured; the ledger folds these into the capture's harness. A
    library that is not installed is recorded as "absent" (analyze.py imports
    perfetto only for a run with a trace), never skipped, so a capture
    analyzed with it never compares with one analyzed without it."""
    from importlib.metadata import PackageNotFoundError, distributions, version

    def _realized(name: str) -> str:
        try:
            return version(name)
        except PackageNotFoundError:
            return "absent"

    tree = {str(d.metadata["Name"]).lower(): d.version for d in distributions() if d.metadata["Name"]}
    return {
        # the installed bytes of each named library and everything it requires
        # (content_sha256): a patched or rebuilt install of one version is
        # another identity, which a version string cannot say
        "content_sha256": {n: dist_content_digest(n) for n in sorted(requirement_closure(names))},
        "python": platform.python_version(),
        # the whole measuring environment, not only the named libraries' own files
        "site_tree_sha256": site_tree_sha256(),
        # the bytes of the interpreter running this script, as the manifest
        # hashes a stack's: two builds of one patch version (optimized or not)
        # report one version string and must still be two identities
        "python_sha256": interpreter_digest(running_interpreter_files()),
        **dict(sorted(tree.items())),
        **{n: _realized(n) for n in names},
        # the behavior-changing part of this instrument's own environment (envs.py)
        "instrument_env": _instrument_env(os.environ),
    }


_REQ_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_closure(names: tuple[str, ...]) -> set[str]:
    """The installed distributions names load: each named one and, transitively,
    every requirement it declares outside an extra. A requirement that is not
    installed is left out (its absence is in the version tree)."""
    from importlib.metadata import PackageNotFoundError, requires

    seen: set[str] = set()
    todo = [_canonical(n) for n in names]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        try:
            reqs = requires(name) or []
        except PackageNotFoundError:
            continue
        seen.add(name)
        for req in reqs:
            spec, _, marker = req.partition(";")
            if "extra" in marker:
                continue  # an optional extra the measuring code does not install
            if m := _REQ_NAME.match(spec.strip()):
                todo.append(_canonical(m.group()))
    return seen


def dist_content_digest(name: str) -> str:
    """sha256 over the installed bytes of a distribution: every file its RECORD
    lists, plus any other file in a directory it installs into (a module added
    to the package after install loads too), by path relative to the install
    root, in sorted order. Bytecode caches are skipped: the interpreter writes
    them on first import, so they would move between stamp and verify without
    any source changing, and they are derived from the files hashed. A listed
    file that is missing is recorded as missing."""
    from importlib.metadata import distribution

    dist = distribution(name)
    root = Path(str(dist.locate_file("")))
    listed = {Path(str(f)) for f in dist.files or []}
    dirs = {f.parent for f in listed if f.parent != Path(".") and ".." not in f.parts}
    paths = set(listed)
    for d in dirs:
        with contextlib.suppress(OSError):
            paths.update(d / c.name for c in (root / d).iterdir() if c.is_file())
    h = hashlib.sha256()
    for rel in sorted(paths, key=lambda q: q.as_posix()):
        if "__pycache__" in rel.parts or rel.suffix == ".pyc":
            continue
        try:
            digest = sha256_file(root / rel)
        except OSError:
            digest = "missing"
        h.update(rel.as_posix().encode() + b"\0" + digest.encode() + b"\n")
    return h.hexdigest()


def _envs() -> Any:
    """envs.py, imported as the package module (ledger, ab.py, analyze.py,
    stack.py) or, for sampler.py and heapdiff.py, which run with the lab
    directory on sys.path, as a sibling."""
    try:
        from scripts.perf.lab import envs
    except ModuleNotFoundError:
        import envs  # type: ignore[no-redef]
    return envs


def _stack() -> Any:
    """stack.py (which imports this module), imported when first needed: as the
    package module, or as a sibling for `manifest.py drift` run as a script."""
    try:
        from scripts.perf.lab import stack
    except ModuleNotFoundError:
        import stack  # type: ignore[no-redef]
    return stack


def _instrument_env(environ: Mapping[str, str]) -> dict[str, str]:
    return _envs().instrument_env(environ)


def harness_stamp(names: tuple[str, ...], lab: Path = HERE) -> tuple[str, dict[str, Any]]:
    """(harness_digest, measurement_deps): the identity an instrument stamps.
    Each instrument takes it BEFORE it loads the code it measures with (a
    trace, snapshot or sample is read by whatever is on disk then) and again
    just before it writes its result; harness_moved names any difference, and
    a result whose identity moved carries none (UNKNOWN, refused by the ledger)."""
    return harness_digest(lab), measurement_deps(names)


def harness_moved(before: tuple[str, Mapping[str, Any]], after: tuple[str, Mapping[str, Any]]) -> str | None:
    """Why the identity taken after measuring is not the one taken before, or None."""
    if before[0] != after[0]:
        return f"the lab code changed while the instrument ran ({before[0][:12]} -> {after[0][:12]})"
    moved = sorted(k for k in before[1].keys() | after[1].keys() if before[1].get(k) != after[1].get(k))
    if moved:
        return f"the instrument's libraries or environment changed while it ran: {', '.join(moved)}"
    return None


def running_interpreter_files() -> list[str]:
    """The files _PY_FILES names for the interpreter running this process
    (its base executable and, for a shared build or on Windows, the libpython
    it loads), evaluated in-process so the base-executable and DLL rules live
    in one place. Raises RuntimeError when the libpython cannot be found."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(_PY_FILES, {"__name__": "_perflab_py_files"})  # our own constant, not input
    except SystemExit as e:
        raise RuntimeError(f"cannot identify the running interpreter: {e.code}") from None
    return list(json.loads(out.getvalue())[1])


# ARM identifies a core by four MIDR fields, not a name. The kernel prints them
# per processor in /proc/cpuinfo (arch/arm64/kernel/cpuinfo.c) and exposes the
# raw register in sysfs; both are read into one form, so either source names
# the same machine class the same way.
_ARM_FIELDS = ("CPU implementer", "CPU architecture", "CPU variant", "CPU part")


def _arm_core(implementer: int, architecture: int, variant: int, part: int) -> str:
    return f"implementer 0x{implementer:02x} architecture {architecture} variant 0x{variant:x} part 0x{part:03x}"


def _arm_cores(cores: list[str]) -> str:
    """Each distinct core and how many there are, sorted: a big.LITTLE SoC is
    its clusters, and core order in the listing is not part of the identity."""
    counts: dict[str, int] = {}
    for c in cores:
        counts[c] = counts.get(c, 0) + 1
    return "ARM " + " + ".join(f"{c} x{n}" for c, n in sorted(counts.items()))


def cpu_model_from_cpuinfo(text: str) -> str | None:
    """The processor identity in /proc/cpuinfo text, or None when it names none.
    x86 (and ARM kernels that print one): "model name". ARM: every processor
    block's implementer, architecture, variant and part (_arm_cores), with the
    model name and the board's "Hardware" or "Model" line appended when the
    kernel prints them. Serial and revision lines are never used: a serial is
    one board, not a machine class. A board line alone is used only when
    nothing else names the processor."""
    blocks: list[dict[str, str]] = [{}]
    tail: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not sep or not value:
            continue
        if key == "processor" and blocks[-1]:
            blocks.append({})
        (tail if key in {"Hardware", "Model"} else blocks[-1])[key] = value
    name = next((b["model name"] for b in blocks if b.get("model name")), None)
    board = tail.get("Model") or tail.get("Hardware")
    cores = []
    for b in blocks:
        if all(b.get(f) for f in _ARM_FIELDS):
            try:
                cores.append(_arm_core(*(int(b[f], 0) for f in _ARM_FIELDS)))
            except ValueError:
                return None  # a field the format does not allow: unreadable, never a guess
    if cores:
        extra = [x for x in (name, board) if x]
        return _arm_cores(cores) + (f" ({'; '.join(extra)})" if extra else "")
    return name or board


def cpu_model_from_midr(registers: list[str]) -> str | None:
    """The ARM identity from sysfs midr_el1 values (one per online CPU,
    /sys/devices/system/cpu/cpu*/regs/identification/midr_el1), in the form
    cpu_model_from_cpuinfo gives. arm64 has architecture field 0xf ("see the
    ID registers"), which the kernel prints as 8, so it is read as 8 here too."""
    cores = []
    for raw in registers:
        try:
            v = int(raw.strip(), 0)
        except ValueError:
            return None
        arch = (v >> 16) & 0xF
        cores.append(_arm_core((v >> 24) & 0xFF, 8 if arch == 0xF else arch, (v >> 20) & 0xF, (v >> 4) & 0xFFF))
    return _arm_cores(cores) if cores else None


def cpu_model() -> str | None:
    """The processor's identity as the OS reports it, or None when no source
    on this host names it (build() then records it UNKNOWN, which never compares).
    Linux: /proc/cpuinfo (cpu_model_from_cpuinfo), else sysfs MIDR registers
    on ARM (cpu_model_from_midr); macOS (Intel and Apple Silicon):
    sysctl machdep.cpu.brand_string, else hw.model; Windows: the registry's
    ProcessorNameString, else platform.processor()."""
    system = platform.system()
    if system == "Linux":
        try:
            found = cpu_model_from_cpuinfo(Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            found = None
        if found:
            return found
        regs = sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*/regs/identification/midr_el1"))
        try:
            return cpu_model_from_midr([r.read_text(encoding="ascii") for r in regs])
        except OSError:
            return None
    if system == "Darwin":
        for key in ("machdep.cpu.brand_string", "hw.model"):
            try:
                if found := _out(["/usr/sbin/sysctl", "-n", key]):
                    return found
            except (OSError, subprocess.CalledProcessError):
                continue
        return None
    if system == "Windows":
        import winreg  # type: ignore[import-not-found]  # platform seam: Windows only

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                found = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            found = ""
        return found or platform.processor().strip() or None
    return None


def _git(git: str, wt: Path, *args: str) -> bytes:
    # envs.git_env: an inherited GIT_DIR, GIT_WORK_TREE or GIT_INDEX_FILE would
    # make `git -C wt` read another repository's head and dirty state
    env = _envs().git_env(os.environ)
    return subprocess.run([git, "-C", str(wt), *args], capture_output=True, check=True, env=env).stdout


def dirty_digest(git: str, wt: Path) -> str | bool:
    """False for a tree stack.py tree_identity calls clean, else its content
    hash: `git diff HEAD --binary` plus every file outside the index a stack
    could execute (stack.counted_untracked: untracked code, and gitignored
    files a process can still load). The one rule, shared with `up`'s tree
    identity and `verify`, so the manifest and the tree never disagree on
    whether the code under test changed. git is the recorded executable the
    caller resolved; the rule launches git by its own resolved path."""
    del git  # stack.tree_identity resolves git itself, through envs.git_env
    tree = _stack().tree_identity(wt)
    return str(tree["content"]) if tree["dirty"] else False


def _out(argv: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, cwd=cwd).stdout.strip()


def browser_identity(node: str, chrome: str | None) -> dict[str, str]:
    """The Chromium a capture launches (browser-id.mjs resolves it exactly as
    the driver does, and reads its version from a real launch)."""
    argv = [node, str(HERE / "browser-id.mjs"), *(["--chrome", chrome] if chrome else [])]
    found = json.loads(_out(argv).splitlines()[-1])
    return {"executable": str(found["executable"]), "version": str(found["version"])}


# Run by the stack's interpreter: its real executable and, for a shared or
# framework build (and every Windows build), the libpython it loads, where
# most of the interpreter's code lives. A venv's python is only a link
# (POSIX), a copy (--copies) or a redirector launcher under Scripts (Windows,
# uv's .venv included) for the base install's interpreter, so the executable
# is the base one (sys._base_executable), and on Windows the DLL is the module
# actually loaded (sys.dllhandle), then the base install's, never one assumed
# to sit beside the venv's launcher.
_PY_FILES = """
import json, os, sys, sysconfig
v = sysconfig.get_config_var
exe = os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)
files = [exe]
if v("Py_ENABLE_SHARED") or v("PYTHONFRAMEWORK") or sys.platform == "win32":
    if sys.platform == "win32":
        import ctypes
        dll = "python%d%d.dll" % sys.version_info[:2]
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetModuleFileNameW(ctypes.c_void_p(sys.dllhandle), buf, len(buf))
        cands = ([buf.value] if n else []) + [os.path.join(os.path.dirname(exe), dll), os.path.join(sys.base_prefix, dll)]
    else:
        cands = [os.path.join(v("LIBDIR") or "", v("INSTSONAME") or ""), os.path.join(v("LIBDIR") or "", v("LDLIBRARY") or ""),
                 os.path.join(v("PYTHONFRAMEWORKPREFIX") or "", v("LDLIBRARY") or "")]
    lib = next((os.path.realpath(c) for c in cands if os.path.isfile(c)), None)
    if lib is None:
        sys.exit("shared interpreter build, but its libpython was not found in %r" % cands)
    files.append(lib)
print(json.dumps([sys.version, files]))
"""


def _resolved(program: str) -> Path:
    """The real file a program path names; a bare name would resolve against
    the working directory, so it is refused."""
    p = Path(program)
    if not p.is_absolute():
        raise ValueError(f"{program} is not a resolved path")
    return p.resolve()


def interpreter_digest(files: list[str]) -> str:
    """sha256 over the named files' bytes, in order: two installs that share a
    prefix path and version string but run different bytes differ here."""
    h = hashlib.sha256()
    for f in files:
        h.update(sha256_file(Path(f)).encode())
    return h.hexdigest()


# ------------------------------------------------------------ realized runtime trees
# What a stack runs beyond its tracked code: the interpreter's site-packages,
# vite's node_modules and the browser's install directory. The lockfiles say
# what SHOULD be installed; these say what IS, so a file edited, added or
# replaced in place under an unchanged lockfile still changes the identity.
# One mechanism (tree_fingerprint) serves all three: `up` records each tree in
# the manifest's `runtime` (outside KEYS: absolute paths, shebangs and .pth
# lines differ between worktrees of one lock, so these are not compared across
# captures), and code_drift re-reads them at each capture's start and end.
#
# Measured on a real worktree (Linux, Thu 15 Oct 2026):
#   .venv site-packages  8081 files  325 MB  stat 0.05 s  content 0.75 s
#   node_modules         6697 files  175 MB  stat 0.08 s  content 0.33 s
#   chromium-1228         305 files  396 MB  stat 0.01 s  content 8.5 s first read, 0.4 s cached
# runtime_drift over all three: 1.0 s per check; the install check at `up`
# (install_problems, pnpm store status included): 2.9 s.
# So site-packages and node_modules are content-hashed at every check; the
# browser is content-hashed once at `up` (browser_sha256, which captures
# compare) and re-checked by stat. RESIDUAL RISK of the stat re-check: a
# browser file rewritten in place to the same size with its mtime and inode
# restored passes until the next `up`; that takes a deliberate forgery, not an
# update, a reinstall or an edit.
#
# Skipped, each derived from what is hashed and rewritten by the running stack
# itself: __pycache__ (bytecode the engine writes on first import; a forged .pyc
# matching its source's size and mtime would load: residual risk) and the
# optimizer caches vite and tools write at the top of node_modules while
# serving (TREE_CACHE_DIRS; vite rebuilds them when the lockfile or config
# changes).
TREE_CACHE_DIRS = frozenset({".vite", ".vite-temp", ".cache"})
# Created by uv with the venv itself, owned by no distribution's RECORD.
VENV_OWN_FILES = frozenset({"_virtualenv.py", "_virtualenv.pth"})
_BROWSER_REVISION = re.compile(r"^(?:chromium|chromium_headless_shell|chrome|chromium-tip-of-tree)-\d+$")


def _entry(p: Path, content: bool) -> str:
    """One entry's fingerprint: a symlink by its target (never followed), a
    regular file by the sha256 of its bytes (content) or by size, mtime_ns and
    inode (stat), any other entry by its type."""
    st = p.lstat()
    if stat.S_ISLNK(st.st_mode):
        return f"L {os.readlink(p)}"
    if not stat.S_ISREG(st.st_mode):
        return "D" if stat.S_ISDIR(st.st_mode) else f"? {stat.S_IFMT(st.st_mode)}"
    return f"F {sha256_file(p)}" if content else f"F {st.st_size}:{st.st_mtime_ns}:{st.st_ino}"


def tree_rows(root: Path, *, content: bool, skip_top: frozenset[str] = frozenset()) -> dict[str, str]:
    """Every entry under a directory root, by path relative to it, each with its
    _entry fingerprint. __pycache__ is skipped everywhere and skip_top names
    entries skipped at the top level. A root that is not a directory (a file or
    a link) is one entry, keyed ".". The install baseline compares these rows
    by name, so an added, removed or changed file is named."""
    if not root.is_dir() or root.is_symlink():
        return {".": _entry(root, content)}
    rows: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        top = base == root
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__" and not (top and d in skip_top))
        for name in [*dirnames, *filenames]:
            if top and name in skip_top:
                continue
            p = base / name
            rows[p.relative_to(root).as_posix()] = _entry(p, content)
    return rows


def tree_fingerprint(root: Path, *, content: bool, skip_top: frozenset[str] = frozenset()) -> str:
    """sha256 over every entry under root (tree_rows), in sorted path order. A
    root that does not exist is "absent"; a root that is a file is that file."""
    if not root.exists() and not root.is_symlink():
        return "absent"
    h = hashlib.sha256()
    for rel, entry in sorted(tree_rows(root, content=content, skip_top=skip_top).items()):
        h.update(f"{rel}\0{entry}\n".encode())
    return h.hexdigest()


def browser_root(executable: Path) -> Path:
    """The browser's install directory: the Playwright revision directory
    (chromium-NNNN) holding the resolved executable; else the macOS .app
    bundle; else the executable's own directory when it holds Chromium's
    resource paks (a system install, whose libraries and paks sit beside the
    launcher); else the executable alone, never a shared directory such as
    /usr/bin."""
    real = executable.resolve()
    for parent in real.parents:
        if _BROWSER_REVISION.match(parent.name):
            return parent
    for parent in real.parents:
        if parent.suffix == ".app":
            return parent
    if any(real.parent.glob("*.pak")):
        return real.parent
    return real


_PY_SITE = """
import json, sysconfig
print(json.dumps(sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})))
"""


def site_dirs(python: Path) -> list[Path]:
    """The site-packages directories of the stack's interpreter."""
    return [Path(p) for p in json.loads(_out([str(python), "-c", _PY_SITE]))]


def runtime_trees(wt: Path, python: Path, browser: Path) -> dict[str, dict[str, str]]:
    """Each realized runtime tree's root and fingerprint (see above)."""
    trees = {f"site:{p}": {"root": str(p), "mode": "content"} for p in site_dirs(python)}
    trees["node_modules"] = {"root": str(wt / "apps/webui/frontend/node_modules"), "mode": "content"}
    trees["browser"] = {"root": str(browser_root(browser)), "mode": "stat"}
    for t in trees.values():
        t["fingerprint"] = _tree(t)
    return trees


def _tree(t: Mapping[str, str]) -> str:
    skip = TREE_CACHE_DIRS if Path(t["root"]).name == "node_modules" else frozenset()
    return tree_fingerprint(Path(t["root"]), content=t["mode"] == "content", skip_top=skip)


def runtime_drift(recorded: Mapping[str, Mapping[str, str]]) -> list[str]:
    """The runtime trees whose fingerprint no longer matches the one `up` recorded."""
    return [name for name, t in sorted(recorded.items()) if _tree(t) != t["fingerprint"]]


def site_problems(site: Path) -> list[str]:
    """Why the installed distributions in site are not what their installer
    wrote: a file whose bytes no longer match the sha256 its RECORD lists
    (uv verified each wheel against uv.lock before installing it, so RECORD
    is the lock's content), a listed file missing, or a file no RECORD owns
    (an added module or .pth loads too). Bytecode caches are not checked
    (__pycache__, above). [] when every file is accounted for."""
    from importlib.metadata import distributions

    owned: set[str] = set()
    problems: list[str] = []
    for d in distributions(path=[str(site)]):
        root = Path(str(d.locate_file("")))
        for f in d.files or []:
            p = Path(os.path.normpath(root / str(f)))
            owned.add(str(p))
            if not f.hash:
                continue
            try:
                got = base64.urlsafe_b64encode(hashlib.new(f.hash.mode, p.read_bytes()).digest()).rstrip(b"=")
            except OSError:
                problems.append(f"{f} is missing")
                continue
            if got.decode() != f.hash.value:
                problems.append(f"{f} differs from its RECORD")
    for dirpath, dirnames, filenames in os.walk(site):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            p = os.path.normpath(os.path.join(dirpath, name))
            if p not in owned and not (Path(dirpath) == site and name in VENV_OWN_FILES):
                problems.append(f"{os.path.relpath(p, site)} is owned by no installed distribution")
    return sorted(problems)


def _yaml_block(text: str, key: str) -> dict[str, str]:
    """The `name: value` lines of one top-level block of a pnpm-workspace.yaml
    (a flat map; nothing else in it is read)."""
    out: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            inside = line.rstrip() == f"{key}:"
            continue
        if inside:
            k, _, v = line.strip().partition(":")
            out[k.strip().strip("'\"")] = v.strip()
    return out


def _package_name(pkg: str) -> str:
    """The package name in a pnpm id: `esbuild@0.25.12`, `@scope/x@1(patch_hash=..)`
    or a .pnpm directory name such as `@scope+x@1_peer`."""
    pkg = pkg.split("(", 1)[0]
    if pkg.startswith("@"):
        return "@" + pkg[1:].replace("+", "/", 1).split("@", 1)[0]
    return pkg.split("@", 1)[0]


def pnpm_exempt(workspace_yaml: str) -> frozenset[str]:
    """Packages pnpm itself changes after fetching them, as the worktree's own
    tracked pnpm-workspace.yaml configures: those it builds (allowBuilds) and
    those it patches (patchedDependencies). Their installed bytes differ from
    the store's by design, so the install check leaves them to the patch file
    and the build (both in the tree identity) and to the runtime fingerprint."""
    built = {k for k, v in _yaml_block(workspace_yaml, "allowBuilds").items() if v == "true"}
    patched = {_package_name(k) for k in _yaml_block(workspace_yaml, "patchedDependencies")}
    return frozenset(built | patched)


def pnpm_modified(store_status: str) -> list[str]:
    """The packages `pnpm store status` reports mutated (its output, read
    whether or not it exits nonzero)."""
    lines = store_status.splitlines()
    if not any("These packages are modified" in line for line in lines):
        return []
    start = next(i for i, line in enumerate(lines) if "These packages are modified" in line) + 1
    out = []
    for line in lines[start:]:
        if not line.strip():
            break
        out.append(line.strip())
    return out


def exempt_names(wt: Path) -> frozenset[str]:
    """pnpm_exempt for the worktree's own tracked pnpm-workspace.yaml."""
    ws = wt / "apps/webui/frontend/pnpm-workspace.yaml"
    return pnpm_exempt(ws.read_text(encoding="utf-8") if ws.exists() else "")


# A package's own node_modules under .pnpm is not its content: pnpm installs
# dependencies beside a package, never inside it, and writes the package's
# .bin shims there only while running its build. A stack whose build ran keeps
# them; one that pnpm restored from its side-effects cache (a later install
# from the same store) does not, though both hold the same built bytes.
_EXEMPT_SKIP_TOP = frozenset({"node_modules"})


def exempt_packages(nm: Path, names: frozenset[str]) -> dict[str, str]:
    """The installed content (tree_fingerprint, content) of every version of each
    exempt package, keyed by its directory under node_modules/.pnpm, without the
    package's own nested node_modules (_EXEMPT_SKIP_TOP)."""
    pnpm = nm / ".pnpm"
    out: dict[str, str] = {}
    for name in sorted(names):
        for d in sorted(pnpm.glob(f"*/node_modules/{name}")):
            out[d.relative_to(pnpm).as_posix()] = tree_fingerprint(d, content=True, skip_top=_EXEMPT_SKIP_TOP)
    return out


def exempt_content(wt: Path) -> str:
    """sha256 over the installed bytes of each exempt package (exempt_packages:
    pnpm builds or patches it, so its files are not the store's). These bytes
    are a dependency's identity: verdict lets two stacks differ in them only
    when their pnpm lockfiles differ, and code_drift rechecks this stack's own
    bytes after `up`. Each stack's bytes are also checked against the clean
    install of its lock (install_baseline, which compares every node_modules path)."""
    nm = wt / "apps/webui/frontend/node_modules"
    h = hashlib.sha256()
    if not nm.is_dir():
        h.update(b"no node_modules\n")
    else:
        for rel, digest in sorted(exempt_packages(nm, exempt_names(wt)).items()):
            h.update(f"{rel}\0{digest}\n".encode())
    return h.hexdigest()


def pinned_content(sites: list[Path]) -> str:
    """sha256 over the venv's own files (VENV_OWN_FILES), by name with absent
    marked, in each site. uv writes these with no RECORD, so nothing vouches for
    them but their pinned bytes: two stacks compare equal only when these match
    (verdict refuses a difference), and code_drift rechecks them after `up`.
    The exempt packages are exempt_content, a dependency's identity, not here."""
    h = hashlib.sha256()
    for site in sorted(sites, key=str):
        for name in sorted(VENV_OWN_FILES):
            # by name, not path: each stack's venv lives somewhere else
            f = site / name
            h.update(f"{name}\0{sha256_file(f) if f.is_file() else 'absent'}\n".encode())
    return h.hexdigest()


# Entries a locked install and the node runs it causes write under node_modules
# that are not package content: pnpm's own metadata at the top (paths and times
# that differ per machine), and the .bin shims (already a residual: not verified).
INSTALL_SKIP_TOP = frozenset({".modules.yaml", ".pnpm-workspace-state-v1.json", ".bin"})
# .bin and .tmp at any depth: the bin links a package's own dependencies get, and
# the node compile cache a package's script leaves (measured: a live esbuild
# tree carries one that a clean install does not).
INSTALL_SKIP_PARTS = frozenset({".bin", ".tmp"})


def install_listing(nm: Path) -> dict[str, str]:
    """The content of every file and link under node_modules that install_baseline
    compares, by path (directories are not listed: their files are), less the
    entries INSTALL_SKIP_TOP and INSTALL_SKIP_PARTS exclude."""
    rows = tree_rows(nm, content=True, skip_top=TREE_CACHE_DIRS | INSTALL_SKIP_TOP)
    return {k: v for k, v in rows.items() if v != "D" and not INSTALL_SKIP_PARTS & set(k.split("/"))}


def seed_listing(fe: Path, pnpm: str) -> dict[str, str]:
    """install_listing of a disposable clean install of the frontend's own lock
    (pnpm install --frozen-lockfile, in a fresh directory, as `up` installs). This is
    the trusted baseline: pnpm built it from the lockfile and checked it, and it
    is never read from the tree being checked. A failed clean install raises
    ValueError, so nothing is trusted from a worktree it did not build."""
    with tempfile.TemporaryDirectory(prefix="perflab-seed-") as tmp:
        # the whole frontend but its node_modules: the workspace's patch files and
        # any other input the lock's install reads must be present, or pnpm fails
        seed = Path(tmp) / "frontend"
        shutil.copytree(fe, seed, ignore=shutil.ignore_patterns("node_modules"))
        out = subprocess.run(
            [pnpm, "install", "--frozen-lockfile"],
            cwd=seed,
            env=_envs().install_env(os.environ),
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            tail = (out.stderr or out.stdout).strip()[-300:]
            raise ValueError(
                f"cannot seed the install baseline: a clean locked install failed (exit {out.returncode}): {tail}"
            )
        return install_listing(seed / "node_modules")


def install_baseline(fe: Path, pnpm: str, baseline: Path) -> list[str]:
    """Compares fe's node_modules with the trusted clean install of its lock, path
    by path, and names every path added, removed or changed. The trusted listing
    comes from seed_listing the first time and is cached at baseline; it is never
    recorded from the tree being checked, so a file present before the first
    check is refused, not blessed (the cache key carries a version, so a cache
    written another way is never read)."""
    if not baseline.is_file():
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps(seed_listing(fe, pnpm), sort_keys=True), encoding="utf-8")
    trusted: dict[str, str] = json.loads(baseline.read_text(encoding="utf-8"))
    now = install_listing(fe / "node_modules")
    added = sorted(set(now) - set(trusted))
    removed = sorted(set(trusted) - set(now))
    changed = sorted(k for k in set(now) & set(trusted) if now[k] != trusted[k])
    return (
        [f"node_modules: {p} was added since the clean install of the lock" for p in added]
        + [f"node_modules: {p} was removed since the clean install of the lock" for p in removed]
        + [f"node_modules: {p} was changed since the clean install of the lock" for p in changed]
    )


def install_problems(wt: Path, sites: list[Path], pnpm: str | None, baseline_dir: Path) -> list[str]:
    """Why the realized installs are not what the lockfiles name, checked at
    `up` right after the locked installs (neither `uv sync --locked` nor
    `pnpm install --frozen-lockfile` re-checks installed bytes: both report
    an edited file as already up to date).
    Python: site_problems (each file against its RECORD, no unowned file).
    node: `pnpm store status` checks every file pnpm indexes, whatever the
    import method (measured here: a copy or clone install has link count 1, an
    in-place edit is reported, exit 1, by name, and a clean install reads
    "Packages in the store are untouched", exit 0). It does NOT see a file
    added under an installed package, so every node_modules path is also
    compared with a clean install of the same lock (install_baseline, kept in
    baseline_dir per worktree and lockfile). Exempt packages are compared as
    part of that, and verdict decides how far two stacks may differ in them. A
    status failure with no modified package is a refusal, never a pass.
    pnpm None skips node (no node_modules to check)."""
    problems = [f"{site.name}: {p}" for site in sites for p in site_problems(site)]
    nm = wt / "apps/webui/frontend/node_modules"
    if pnpm is not None and nm.is_dir():
        exempt = exempt_names(wt)
        out = subprocess.run(
            [pnpm, "store", "status"],
            cwd=nm.parent,
            env=_envs().install_env(os.environ),
            capture_output=True,
            text=True,
            check=False,
        )
        modified = pnpm_modified(out.stdout + out.stderr)
        problems += [
            f"node_modules: {pkg} was modified after pnpm fetched it"
            for pkg in modified
            if _package_name(pkg) not in exempt
        ]
        if out.returncode != 0 and not modified:
            problems.append(f"node_modules: pnpm store status failed (exit {out.returncode}), so nothing is verified")
        if not problems:
            lock = nm.parent / "pnpm-lock.yaml"
            key = hashlib.sha256(
                f"seed-v2\0{nm.resolve()}\0{sha256_file(lock) if lock.is_file() else 'no lock'}".encode()
            )
            problems += install_baseline(nm.parent, pnpm, baseline_dir / f"{key.hexdigest()}.json")
    return problems


def build(
    wt: Path, fixture: Path, python: Path, node: str, git: str, browser: Mapping[str, str], *, library_mode: str
) -> dict[str, Any]:
    """The manifest of a stack on worktree wt serving the fixture copy at
    fixture, run by python and node, profiled with browser. Raises
    CalledProcessError or OSError when a field cannot be measured, and
    ValueError when the realized installs are not what the lockfiles name
    (install_problems): `up` refuses rather than measure them."""
    nm = wt / "apps/webui/frontend/node_modules"
    pnpm = _stack()._exe("pnpm") if nm.is_dir() else None
    problems = install_problems(wt, site_dirs(python), pnpm, _stack()._scratch() / "install-baselines")
    if problems:
        shown = "; ".join(problems[:8]) + (f"; and {len(problems) - 8} more" if len(problems) > 8 else "")
        raise ValueError(
            f"the installed packages are not what the lockfiles name: {shown}. "
            "Reinstall them (uv sync --locked --reinstall; pnpm install --force), clearing a tampered uv or pnpm cache first"
        )
    py = json.loads(_out([str(python), "-c", _PY_FILES]))
    m: dict[str, Any] = {
        "commit": _git(git, wt, "rev-parse", "HEAD").decode().strip(),
        "dirty": dirty_digest(git, wt),
        "pnpm_lock": sha256_file(wt / "apps/webui/frontend/pnpm-lock.yaml"),
        "uv_lock": sha256_file(wt / "uv.lock"),
        "browser_version": browser["version"],
        # the whole install the executable runs from (browser_root): its
        # libraries and resource paks are browser code too
        "browser_sha256": tree_fingerprint(browser_root(Path(browser["executable"])), content=True),
        "python_version": py[0],
        "python_sha256": interpreter_digest(py[1]),
        "node_version": _out([node, "--version"]),
        "node_sha256": sha256_file(_resolved(node)),
        "fixture": tree_digest(fixture),
        "library_mode": library_mode,
        **host_fields(),
        "pinned_content": pinned_content(site_dirs(python)),
        "exempt_content": exempt_content(wt),
    }
    # a field build could not measure at all is an error; an unreadable host
    # field is recorded UNKNOWN (host_fields) and only voids comparisons
    missing = [k for k in KEYS if m.get(k) is None]
    if missing:
        raise ValueError(f"stack manifest lacks {', '.join(missing)}")
    # outside KEYS: re-checked by code_drift, never compared across captures
    m["runtime"] = runtime_trees(wt, python, Path(browser["executable"]))
    return m


def code_drift(state: Mapping[str, Any]) -> list[str]:
    """The manifest keys whose live value no longer matches what `up`
    recorded: the code under test in the stack's worktree, and the bytes of
    the Chromium captures launch. The stack serves the worktree live (Vite),
    so an edit, checkout or lockfile change after `up` would be measured under
    the old provenance, and a reinstalled or patched browser, or a package
    edited in .venv or node_modules, keeps every version string while running
    other bytes (runtime_drift names those as runtime:<tree>); a capture
    checks this when it starts and when it ends. A manifest that recorded no
    runtime trees cannot be vouched for, and drifts as "runtime"."""
    wt, git, m = Path(state["worktree"]), str(state["git"]), state["manifest"]
    live = {
        "commit": _git(git, wt, "rev-parse", "HEAD").decode().strip(),
        "dirty": dirty_digest(git, wt),
        "pnpm_lock": sha256_file(wt / "apps/webui/frontend/pnpm-lock.yaml"),
        "uv_lock": sha256_file(wt / "uv.lock"),
    }
    runtime_now = m.get("runtime") if isinstance(m.get("runtime"), Mapping) else {}
    sites = [Path(t["root"]) for k, t in runtime_now.items() if k.startswith("site:")]
    live["pinned_content"] = pinned_content(sites)
    live["exempt_content"] = exempt_content(wt)
    drift = [k for k in sorted(live) if live[k] != m[k]]
    runtime = m.get("runtime")
    if not isinstance(runtime, Mapping) or not runtime:
        return [*drift, "runtime"]
    return drift + [f"runtime:{name}" for name in runtime_drift(runtime)]


def main(argv: list[str]) -> int:
    """`manifest.py drift <stack state file>`: exit 0 when the worktree and
    browser still match the stack's manifest, 1 naming the drifted keys, 2 when it cannot measure."""
    if len(argv) != 2 or argv[0] != "drift":
        print("usage: manifest.py drift <stack state file>", file=sys.stderr)
        return 2
    try:
        keys = code_drift(json.loads(Path(argv[1]).read_text(encoding="utf-8")))
    except (OSError, KeyError, ValueError, subprocess.CalledProcessError) as e:
        print(f"[UNKNOWN] cannot measure drift: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    if keys:
        print(f"changed since stack up: {', '.join(keys)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
