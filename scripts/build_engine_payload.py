"""Stage a self-contained Open DJ engine payload for the desktop bundle.

WHAT THIS PRODUCES

A directory that boots the FastAPI engine on a Mac with no repo, no uv, no
Homebrew and no dev toolchain::

    payload/
      manifest.json          identity + sizes + digests + the safety scans
      bin/opendj-engine      the ONE entry point the Tauri shell knows about
      bin/opendj             the agent CLI launcher (same env contract)
      bin/odj-audio          the Rust audio engine, device-output build (NAE-13)
      runtime/               relocatable CPython (python-build-standalone)
      pylib/                 the engine's dependency closure
      app/apps/...           engine source, including the built SPA

MECHANISM, AND WHY NOT PYINSTALLER

python-build-standalone plus a ``--target`` dependency tree plus a verbatim
copy of the engine source. Every module is a real file at a real path, so:

- ``Path(__file__).resolve().parents[2]`` keeps working. The engine derives
  PROJECT_ROOT and FRONTEND_BUILD_DIR that way, and a frozen/zipped importer
  breaks both. PyInstaller would need the engine rewritten to suit the
  packer, which is the tail wagging the dog.
- an import failure is a missing file you can ``ls``, not an opaque
  "ModuleNotFoundError" from a static-analysis miss. PyInstaller discovers
  imports by analysis, and this dependency set is full of string-keyed
  dynamic imports (uvicorn protocol classes, sqlalchemy dialects,
  pyrekordbox kaitai structs) whose misses surface on the TESTER's machine
  at runtime. Deferring a build failure to a user is the opposite of the
  house rule.

Lane A reached the same conclusion. This builder adds the three things that
make the payload provable rather than merely produced (see :func:`verify`):
an import + route-table check under the payload's own interpreter, a
linked-library scan, a runtime-loaded-library scan, and a desktop icon-set
check so a dmg cannot ship with a partial ``icon.icns``.

Requirements:

- ✔︎ ✅ 🎯 An unset lane label builds the plain product (Open DJ,
  com.opendj.desktop, OPS-08); a set label suffixes every name and an unsafe
  label is refused, never sanitised. -> :func:`build`,
  ``desktop_lane_config.validate_label``
- ✔︎ ✅ 🎯 A missing or stale SPA build fails the build; it is never rebuilt
  silently. -> :func:`assert_spa_is_fresh`
- ✔︎ ✅ 🎯 The payload carries exactly one Signalsmith worklet asset.
  -> :func:`sole_stretch_asset`
- ✔︎ ✅ 🎯 No shipped Mach-O file links anything outside the bundle, /usr/lib
  and /System. -> :func:`scan_linked_libraries`
- ✔︎ ✅ 🎯 Every ctypes runtime-load site in the payload is classified as
  bundled or provably unreachable. -> :func:`scan_runtime_loaded_libraries`
- ✔︎ ✅ 🎯 The manifest stamps git sha, branch, dirty flag and build time, and
  the build fails when git cannot answer. -> :func:`git_identity`
- ✔︎ ✅ 🎯 The staged runtime's major.minor equals the repo's .python-version
  pin; any other interpreter -- uv-resolved or --runtime-source -- is
  refused before dependencies install. -> :func:`assert_runtime_matches_pin`
- ✔︎ ✅ 🎯 Every staged ``*.pth`` imports a module the payload contains, and
  ``python3 -c pass`` writes nothing to stderr. -> :func:`verify_pth_imports`,
  :func:`verify_python_startup_stderr` (INSTALL-22, issue #2752)

Acceptance tests:

- [if] MDT_LANE_LABEL is unset [then] the manifest identity carries
  product_name "Open DJ", bundle_identifier "com.opendj.desktop" and an
  empty lane_label, [else ⛔️].
- [if] MDT_LANE_LABEL is an unsafe value like "lane b" [then] the build
  exits non-zero, never a sanitised name, [else ⛔️].
- [if] a frontend source file is newer than build/ [then] the build exits
  non-zero naming both mtimes, [else ⛔️].
- [if] a staged .so links /opt/homebrew [then] the build exits non-zero
  naming the file and the offending path, [else ⛔️].
- [if] the payload's only Mach-O is the extensionless interpreter [then] the
  linked-library scan still inspects it, [else ⛔️].
- [if] a staged .so is a dylib rather than a bundle [then] its own install
  name is not reported as a dependency, [else ⛔️].
- [if] file(1) cannot classify a candidate [then] the build exits non-zero
  rather than dropping it from the scan, [else ⛔️].
- [if] a staged .py calls find_library for a library that is neither bundled
  nor on the classified list [then] the build exits non-zero naming the call
  site, [else ⛔️].
- [if] the tree is dirty at build time [then] manifest identity.git_dirty is
  true, [else ⛔️].
- [if] the staged runtime reports 3.14.x while .python-version pins 3.11.15
  [then] the build exits non-zero naming both versions, [else ⛔️].
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from apps.sync_hub import first_run as cloudsync_first_run
    from apps.sync_hub.config import CloudSyncConfigError
    from scripts import payload_beatgrid
    from scripts.desktop_lane_config import (
        LaneLabelError,
        lane_identifier,
        lane_product_name,
        validate_label,
    )
    from scripts.payload_launchers import write_payload_launcher
    from scripts.payload_runtime_allowlist import RUNTIME_LOAD_ALLOWLIST
except ModuleNotFoundError as exc:
    if exc.name in ("scripts", "apps"):
        raise SystemExit("uv run --no-sync python -m scripts.build_engine_payload") from None
    raise

MANIFEST_NAME: str = "manifest.json"
MANIFEST_SCHEMA: int = 1
#: Build-time source of the first-run default hub (#3870). The value lands in
#: manifest.json under ``cloudsync.default_hub_url``; the engine reads it via
#: apps/sync_hub/first_run.py. Runtime MDT_CLOUDSYNC_DEFAULT_HUB_URL wins over it.
DEFAULT_HUB_URL_ENV: str = "OPENDJ_CLOUDSYNC_DEFAULT_HUB_URL"
MANIFEST_KIND: str = "opendj-engine-payload"

LAUNCHER_RELATIVE: str = "bin/opendj-engine"
CLI_LAUNCHER_RELATIVE: str = "bin/opendj"
PYTHON_LAUNCHER_RELATIVE: str = "bin/opendj-python"

# The engine's Rust waveform extension is a setuptools-rust ext-module of the
# PROJECT wheel, and the payload's closure comes from ``uv export
# --no-emit-project`` -- which by definition excludes it -- while app code is
# staged as plain source. Without a dedicated staging step every payload
# therefore serves waveforms through the NumPy fallback unless the native
# extension is staged explicitly. The standalone maturin crate below builds
# the identical ``_rb_waveform_native`` module without dragging a second copy
# of ``apps`` into pylib.
WAVEFORM_CRATE_MANIFEST: str = "apps/webui/server/native/waveform/Cargo.toml"
AUDIO_ENGINE_CRATE: str = "apps/audio-engine"
AUDIO_ENGINE_RELATIVE: str = "bin/odj-audio"
WAVEFORM_WHEEL_CHECKER: str = "scripts/check_waveform_native_wheel.py"

# The env var the launcher exports so the engine can find its own manifest.
# Absent means "running from a repo checkout", which /api/v1/build-info
# answers from live git instead.
MANIFEST_ENV: str = "OPENDJ_PAYLOAD_MANIFEST"

# Copied verbatim into the payload. Everything under apps/ except the
# frontend's source tree, which is replaced by its built output alone.
APP_SOURCE_ROOTS: tuple[str, ...] = ("apps",)

# META-06 glossary: staged beside apps/ so glossary.MAP_PATH (parents[2] from
# apps/open_dj/glossary.py) resolves in both repo checkout and payload layouts.
GLOSSARY_MAP_REPO_RELATIVE: Path = Path("open-dj/synonym-map.json")
GLOSSARY_MAP_PAYLOAD_RELATIVE: Path = Path("open-dj/synonym-map.json")
VOCAL_WORKER_REPO_RELATIVE: Path = Path("scripts/vocal_region_worker.py")
VOCAL_WORKER_PAYLOAD_RELATIVE: Path = Path("scripts/vocal_region_worker.py")

# Frontend inputs whose mtime decides whether build/ is stale.
SPA_SOURCE_PATHS: tuple[str, ...] = (
    "src",
    "static",
    "package.json",
    "pnpm-lock.yaml",  # the bundled closure, which OSSPUB-05 inventories (Codex P1, PR #4853)
    "patches",
    "svelte.config.js",
    "vite.config.ts",
)

# Declared dependencies the payload deliberately does NOT embed, with the
# reason each is safe to drop. An entry here is a claim the build re-checks:
# the import + route-table verification runs against the pruned closure, so
# a wrong exclusion fails the build rather than the tester's launch.
EXCLUDED_DEPENDENCIES: dict[str, str] = {
    "pyacoustid": (
        "audio fingerprinting only. Its chromaprint submodule resolves "
        "libchromaprint through ctypes.util.find_library at import time and "
        "the library is not bundled anywhere (find_library('chromaprint') "
        "already returns None on the build Mac), and acoustid.fingerprint_file "
        "additionally shells out to an fpcalc binary that is not bundled "
        "either. Both importers in this tree (apps/shared/fingerprints.py, "
        "apps/sync/fingerprint.py) import it INSIDE a function, no route in "
        "the engine's table reaches either, and the payload verification "
        "builds the whole route table without it. Embedding it would ship a "
        "guaranteed-dead code path plus an unresolvable find_library site. "
        "Duplicate detection does not need it: apps/shared/fingerprints.py "
        "fingerprints through the bundled odj-audio engine "
        "(ADR-NEW-on-device-audio-fingerprints) and uses pyacoustid only as a "
        "dev-checkout fallback."
    ),
}

# Optional-dependencies GROUPS this build DOES request, keyed by extra name
# with the reason. Audited in the opposite direction to
# OMITTED_OPTIONAL_EXTRAS: an entry here is checked for PRESENCE in the
# locked closure, because an extra silently dropping out of the export is
# the same class of unnoticed change as one silently leaking in.
REQUESTED_OPTIONAL_EXTRAS: dict[str, str] = {
    "analysis": (
        "scipy/librosa/soundfile back the Phase 6 analysis pipeline behind "
        "apps.analysis. The payload used to omit them on the ground that "
        "'the default route table never reaches that module', which stopped "
        "being true: the engine builds /api/v1/analysis-queue and "
        "/api/v1/ingest/refresh, and analysis_autostart.arm_from_environ "
        "calls backends.default_backend_installed() on the boot path. With "
        "the extra omitted that call returned False on every installed app, "
        "so the analyze-on-import loop was never armed and a folder import "
        "-- the path with no rekordbox ANLZ to fall back on -- left every "
        "track with no BPM, no key and no beatgrid, permanently."
    ),
    "vocals": (
        "the installed app's local Demucs path runs against the bundled "
        "payload interpreter, so end users do not need a host Python "
        "toolchain; the worker remains lazy and does not enter engine boot"
    ),
    "observability": (
        "sentry-sdk backs apps/shared/telemetry. Until Mon 21 Sep 2026 the "
        "payload omitted it on the ground that an empty SENTRY_DSN no-ops, "
        "which was true and beside the point: a dmg then carried no DSN "
        "either, so no installed engine has ever reported an error. OBS-04 "
        "bakes the ship DSN (scripts/payload_telemetry.py) and a packaged "
        "build defaults telemetry ON, with a data-dir opt-out marker; the "
        "SDK has to be there for any of that to do anything. Costs ~1.5 MB "
        "installed (sentry-sdk plus urllib3, already a transitive dep)."
    ),
}

# Optional-dependencies GROUPS this script deliberately never requests from
# `uv export` (no --extra/--all-extras below), keyed by extra name with the
# reason each omission is a standing decision rather than an oversight.
# "all" is the only unaudited name: it is a pure aggregate of the others
# ("music-dj-tools[tags,analysis,...]") with no packages of its own, so it
# cannot itself leak into the export. Every real extra IS audited here,
# including "dev" -- `--no-dev` disables uv's own dev-dependencies/
# dependency-groups mechanism, NOT a PEP 621 [project.optional-dependencies]
# group merely named "dev"; verified empirically (`uv export --no-dev
# --extra dev` still resolves pytest/httpx2/cryptography), so treating
# --no-dev as covering it was itself an unaudited assumption. Unlike
# EXCLUDED_DEPENDENCIES, these packages never enter the locked closure in
# the first place, so there is nothing for prune_excluded to drop --
# _verify_omitted_extras is the mirror check in the OTHER direction: it
# fails the build if pyproject.toml grows an extra this registry does not
# name, or if a package belonging to an audited extra shows up in the
# export anyway, which is exactly the "nobody re-checked this" failure
# mode issue #795 found for the "tags" extra.
OMITTED_OPTIONAL_EXTRAS: dict[str, str] = {
    "voice": (
        "openwakeword/sounddevice/webrtcvad back Phase 14 voice commands and "
        "are lazy-imported; sounddevice also needs a system PortAudio "
        "(brew install portaudio) this payload never bundles."
    ),
    "cloud": (
        "boto3 backs the Phase 11 cloud replicate feature and is "
        "lazy-imported from apps.cloud only, so the rest of the engine works "
        "with no AWS SDK present in the payload."
    ),
    "spotify": (
        "spotipy backs the Phase 9 Spotify importer and is lazy-imported "
        "from apps.spotify.client, unreachable unless that route is hit."
    ),
    "ai": (
        "scikit-learn backs the sets auto-classifier and is lazy-imported "
        "from apps.sets.classify, unreachable unless that route is hit."
    ),
    "usb-export": (
        "rbox==0.1.7 is GPL-3.0-only and must not ship in this Apache-2.0 "
        "payload. First use fetches it (apps.sync.usb.pioneer.rbox_runtime). "
        "This build never passes --extra usb-export or --extra dev."
    ),
    "dev": (
        "httpx2/mypy/pytest/pytest-asyncio/pytest-xdist are test and "
        "typecheck tooling (see the dev extra's own comment above), never "
        "imported by shipped runtime code. cryptography is ALSO a core "
        "dependency (RS256 for Google id_token enrollment, ADR 12), so "
        "the check subtracts it and it ships. This build never "
        "passes --extra dev, and --no-dev does not exclude a PEP 621 "
        "optional-dependencies group merely named 'dev' -- so this entry, "
        "not --no-dev, is what would catch a future '--extra dev' typo "
        "shipping these unaudited."
    ),
}

# Packages that must never enter a shipped payload no matter HOW they might
# arrive -- a stray direct dependency, a future extra, a transitive pull-in
# of some other package. Unlike OMITTED_OPTIONAL_EXTRAS, which only audits
# extras this build deliberately never requests, this check runs against the
# full resolved closure regardless of which extras were requested, because
# the packages named here can never be safe to ship under any combination.
NEVER_SHIP: dict[str, str] = {
    "rbox": (
        "GPL-3.0-only, so the Apache-2.0 payload must not distribute it. "
        "USB export fetches rbox==0.1.7 on first use (issue #5143)."
    ),
    "madmom": (
        "BSD-3-Clause source code, but every beat/downbeat processor loads "
        "pretrained models from madmom/models, which are CC BY-NC-SA 4.0 "
        "(non-commercial) -- see apps/analysis/backends/__init__.py's "
        "NONSHIPPABLE_BACKENDS registry (NATIVE-08, issue #1480)."
    ),
}


def _assert_never_ship_absent(entries: list[LockedRequirement]) -> None:
    """Fail the build if a NEVER_SHIP package resolved into the closure.

    Runs against the parsed locked export, not pyproject.toml's declared
    strings, so a package arriving through a transitive dependency of some
    other, legitimately-requested package is caught the same as a direct one.
    """
    present = {entry.name for entry in entries} & set(NEVER_SHIP)
    if present:
        reasons = "; ".join(f"{name}: {NEVER_SHIP[name]}" for name in sorted(present))
        raise PayloadBuildError(
            f"the locked export resolved {sorted(present)}, which must never "
            f"ship in any payload regardless of how it arrived -- {reasons}"
        )


# Removed from the CPython install after it is copied. Each entry is dead
# weight for a daemon that never compiles, never opens a Tk window and never
# installs packages at runtime.
RUNTIME_PRUNE_RELATIVE: tuple[str, ...] = (
    # Entries are applied as globs relative to the staged runtime root, so
    # they hold across interpreter minor versions. The list was originally
    # written with exact 3.14 paths from the first build Mac; any OTHER
    # uv-resolved interpreter (CI's 3.11, a fresh machine's 3.13) staged a
    # runtime these exact paths missed -- pip survived into site-packages and
    # the runtime-load scan refused the payload (correctly: pip's vendored
    # truststore/urllib3 carry ctypes loads a tester's machine resolves).
    "BUILD",
    "include",
    "share",
    "bin/idle*",
    "bin/pip*",
    "bin/pydoc*",
    "bin/python*-config",
    "lib/python3.*/site-packages/pip",
    "lib/python3.*/site-packages/pip-*.dist-info",
    # 3.14-EXACT on purpose, do not glob: python-build-standalone's 3.14
    # interpreter statically links libpython (otool -L shows no entry), so
    # dropping the dylib removes 18MB and the one absolute external path.
    # Its 3.11 interpreter LINKS IT DYNAMICALLY (bin/python3.11 references
    # @executable_path/../lib/libpython3.11.dylib and SIGABRTs without it --
    # verified Sat 29 Aug 2026), so a version glob here breaks 3.11 payloads.
    # Inert while .python-version pins a non-3.14 line (the pin is asserted
    # by assert_runtime_matches_pin before this list applies); it stays
    # because the list must hold across minor versions while the pin decides
    # which one ships.
    "lib/libpython3.14.dylib",
    "lib/libtcl9.0.dylib",
    "lib/libtcl9tk9.0.dylib",
    "lib/tcl9.0",
    "lib/tdbc1.1.10",
    "lib/itcl4.3.2",
    "lib/tdbcmysql1.1.10",
    "lib/tdbcodbc1.1.10",
    "lib/tdbcpostgres1.1.10",
    "lib/sqlite3.50.4",
    "lib/thread3.0.4",
    "lib/python3.*/test",
    "lib/python3.*/idlelib",
    "lib/python3.*/tkinter",
    "lib/python3.*/turtledemo",
    "lib/python3.*/ensurepip",
    "lib/python3.*/pydoc_data",
    "lib/python3.*/lib2to3",
    # The 3.13+ interactive REPL. The launcher execs `python3 -m
    # apps.engine_core serve`, and THROUGH 3.13 a -m launch imports no part
    # of _pyrepl (verified on 3.13.3: neither _pyrepl nor any curses module
    # appears in sys.modules). That claim is version-scoped, not general: on
    # 3.14 pdb.py gained a module-scope `import _pyrepl.utils`, and the
    # engine reaches pdb transitively at import time (pyrekordbox ->
    # construct -> construct.debug -> `import pdb`), so a 3.14 runtime with
    # this prune cannot import its own app -- the fail-closed verify refused
    # exactly that payload on Tue 1 Sep 2026. assert_runtime_matches_pin is
    # what keeps the prune sound: an interpreter off the pinned line is
    # refused before it can stage, and moving the pin to 3.14+ means
    # re-deciding this entry, not discovering it. Pruning rather than
    # allowlisting its ctypes sites follows the rule PYLIB_PRUNE_DIR_NAMES
    # states below -- removing the code removes the call sites, so the scan
    # stops having to reason about them. It also drops
    # _pyrepl/_minimal_curses.py, whose find_library/LoadLibrary pair would
    # otherwise need an exemption justified only by "we never launch
    # interactively", a far weaker and more fragile claim than the
    # platform-unreachability the other exemptions rest on.
    "lib/python3.*/_pyrepl",
    "lib/python3.*/config-3.*-darwin",
    # Globbed like the pip entries above: a 3.14-exact spelling here silently
    # stopped matching the moment the staged runtime was any other line.
    # Prune together: distutils-precedence.pth imports _distutils_hack on every
    # interpreter start; keeping the .pth without the package reproduces issue
    # #2752 (_distutils_hack traceback on stderr / engine.log).
    "lib/python3.*/site-packages/_distutils_hack",
    "lib/python3.*/site-packages/distutils-precedence.pth",
)

# Mach-O dependency paths a shipped file may name. Anything else -- a
# Homebrew prefix, /usr/local, or an absolute path into the build machine's
# home -- is a link that exists on this Mac and nowhere else.
ALLOWED_LINK_PREFIXES: tuple[str, ...] = (
    "@rpath/",
    "@loader_path/",
    "@executable_path/",
    "/usr/lib/",
    "/System/Library/",
)

# ctypes call sites the scan understands. A site whose argument is not a
# string literal is recorded with its source line and must still be
# classified; "we could not read it" is not the same as "it is fine".
CTYPES_SITE_PATTERN: re.Pattern[str] = re.compile(
    r"(?P<call>find_library|CDLL|cdll\.LoadLibrary|WinDLL|windll\.LoadLibrary)"
    r"\(\s*(?P<arg>(?P<quote>['\"])(?P<literal>[^'\"]*)(?P=quote))?"
)

# An allowlist key naming a call site rather than a library: "<path>.py:<line>".
LINE_KEY_PATTERN: re.Pattern[str] = re.compile(r"^(?P<path>[^:]+\.py):(?P<line>\d+)$")

# RUNTIME_LOAD_ALLOWLIST (every runtime-loaded library the payload may name,
# keyed by the literal argument or by "<path>.py:<line>" for a dynamic site)
# lives in scripts/payload_runtime_allowlist.py; anything the scan finds that
# is not there fails the build.
#
# The beatgrid runner site's own torch/filelock load sites (NATIVE-10), kept
# beside the module that stages that site.
RUNTIME_LOAD_ALLOWLIST.update(payload_beatgrid.RUNNER_RUNTIME_LOAD_ALLOWLIST)

# Call sites classified by the FILE they live in, because the argument is
# computed rather than literal, or because the module is the loader itself.
# Path suffixes, so the runtime/ or pylib/ prefix does not matter. Every entry
# is a claim about why this site cannot strand a tester.
RUNTIME_LOAD_SITE_REASONS: tuple[tuple[str, str], ...] = (
    (
        "ctypes/util.py",
        "this module IS find_library; the matches are its own definition.",
    ),
    (
        "ctypes/__init__.py",
        "this module IS CDLL; the matches are its own class definition.",
    ),
    (
        "ctypes/_aix.py",
        "AIX-only branch of find_library, unreachable on darwin.",
    ),
    (
        "ctypes/macholib/dyld.py",
        "the dyld search emulation find_library itself calls.",
    ),
    (
        "ctypes/macholib/dylib.py",
        "dylib name parsing used by the dyld emulation.",
    ),
    (
        "ctypes/macholib/framework.py",
        "framework name parsing used by the dyld emulation.",
    ),
    (
        "audioread/macca.py",
        "audioread's macOS decoder resolves its two frameworks through a "
        "helper taking the NAME as a parameter, so the scanner reads the call "
        "as dynamic. The only two callers, four lines below, pass the literals "
        "'AudioToolbox' and 'CoreFoundation'; both are OS frameworks under "
        "/System/Library and both are allowlisted by name above."
    ),
    (
        "llvmlite/binding/ffi.py",
        "llvmlite loads its OWN dylib, resolved from its package directory "
        "(get_library_name() joined to the llvmlite.binding module path), "
        "never from a system search. That file ships: "
        "pylib/llvmlite/binding/libllvmlite.dylib. Proved positively rather "
        "than read: under the payload interpreter with an empty environment, "
        "cwd '/' and sys.path limited to pylib, llvmlite.binding reports "
        "default triple arm64-apple-darwin and a numba @njit function "
        "compiles and returns the right answer."
    ),
    (
        "numba/cuda/cudadrv/libs.py",
        "numba's CUDA driver loader. There is no CUDA on Apple Silicon and "
        "nothing in this closure imports numba.cuda: librosa reaches numba "
        "only through the CPU @jit path. A site with no caller has no library "
        "name for a tester's machine to lack."
    ),
    (
        "numba/np/ufunc/parallel.py",
        "numba's OPTIONAL Intel TBB threading layer, loaded inside a try "
        "whose failure path disables that layer and falls back to the "
        "workqueue backend. libtbb is not bundled and is not expected to be: "
        "the fallback is the shipped configuration, and a numba @njit "
        "function compiles and runs under the payload interpreter with no "
        "TBB present."
    ),
    (
        "scipy/integrate/_quadpack_py.py",
        "not code. The match is inside quad()'s docstring, in the indented "
        "literal block showing a user how to call a ctypes function; the "
        "path in it is the docstring's own placeholder "
        "('/home/.../testlib.*' with a '#use absolute path' comment) and "
        "nothing executes it."
    ),
    (
        "threadpoolctl.py",
        "threadpoolctl INSPECTS libraries that are already loaded: three of "
        "the four sites pass RTLD_NOLOAD, which by definition returns a "
        "handle only if the library is already in the process and never "
        "loads one. The fourth is ctypes.WinDLL, unreachable on darwin. "
        "threadpoolctl ships because scikit-learn declares it, and "
        "scikit-learn ships because librosa declares it."
    ),
    (
        "cffi/api.py",
        "cffi's ABI-mode loader: FFI.dlopen(name) resolves a caller-supplied "
        "library name through ctypes.util.find_library. cffi ships because "
        "cryptography's _rust.abi3.so embeds a cffi API-mode module "
        "(_openssl) whose PyInit imports the _cffi_backend extension; that "
        "path never calls FFI.dlopen, and no other module in the closure "
        "does either (the only .dlopen( caller outside cffi is a numba example "
        "script under numpy/random/_examples that nothing imports). The site "
        "has no caller, so there is no library name for a tester's machine to "
        "lack. The build's verify step proves the good half positively: "
        "cryptography's Rust bindings import and jwt reports has_crypto under "
        "the payload interpreter with sys.path limited to pylib.",
    ),
    (
        "cffi/backend_ctypes.py",
        "cffi's pure-Python fallback backend, selected only when `import "
        "_cffi_backend` fails (cffi/api.py); the payload ships "
        "_cffi_backend.cpython-311-darwin.so, which links only /usr/lib/"
        "libffi.dylib and libSystem, so this module is never instantiated and "
        "its CDLL(path) call is dead.",
    ),
    (
        "truststore/_macos.py",
        "truststore (httpx2 and httpcore2, required by mcp 2.x since PR #2806) "
        "loads exactly two libraries through its _load_cdll(name, path) helper: "
        "Security and CoreFoundation, both OS frameworks under /System/Library/"
        "Frameworks. On macOS 10.16 and later it CDLLs the literal framework "
        "path it was given; find_library(name) runs only below 10.16, which "
        "this app does not support. There is no third-party library for a "
        "tester's machine to lack.",
    ),
    (
        "apps/sync/usb/pioneer/export_workflow.py",
        "CDLL(None) is dlopen(NULL): it binds a symbol (renamex_np) out of "
        "the image already loaded into this process, which on macOS is always "
        "libSystem. No file is searched for, so there is nothing a tester's "
        "machine can be missing.",
    ),
    (
        "objc/_bridgesupport.py",
        "pyobjc. The CDLL(None) call is the same dlopen(NULL) as above; the "
        "LoadLibrary call takes a path pyobjc built from an OS framework's own "
        "Resources/BridgeSupport directory under /System, never a third-party "
        "prefix.",
    ),
    (
        "packaging/_manylinux.py",
        "glibc version probing behind an explicit Linux-only branch; the "
        "manylinux code path cannot run on darwin.",
    ),
    (
        "_ios_support.py",
        "stdlib iOS support, imported only when sys.platform is 'ios'.",
    ),
    (
        "platform.py",
        "the Android branch of platform.android_ver(); 'libc.so' is an "
        "Android soname and the branch is unreachable on darwin.",
    ),
)

# Directories inside the installed dependency tree that no daemon imports.
# Removing them is ~24MB and it also removes their ctypes call sites, so the
# runtime-load scan stops having to reason about test fixtures. If a prune
# ever silently stops matching, the scan fails the build rather than shipping
# an unexamined site.
PYLIB_PRUNE_DIR_NAMES: tuple[str, ...] = ("tests", "PyObjCTest")


class PayloadBuildError(RuntimeError):
    """The payload cannot be staged, or cannot be proven safe to ship."""


# ----- staged shapes -----------------------------------------------------
@dataclass(frozen=True)
class LinkViolation:
    """A shipped Mach-O file naming a path that exists only on this Mac."""

    path: str
    dependency: str

    def describe(self) -> str:
        return f"{self.path} links {self.dependency}"


@dataclass(frozen=True)
class RuntimeLoadSite:
    """A ctypes call that resolves a library while the daemon is running."""

    path: str
    line: int
    call: str
    library: str | None
    source: str

    @property
    def key(self) -> str:
        """What the allowlist is looked up by: a name, or the site itself."""
        if self.library is None:
            return f"{self.path}:{self.line}"
        return self.library

    def describe(self) -> str:
        named = self.library if self.library is not None else "<dynamic>"
        return f"{self.path}:{self.line} {self.call}({named})"


@dataclass
class PayloadReport:
    """What the build proved, carried into the manifest verbatim."""

    linked_libraries_scanned: int = 0
    link_violations: list[LinkViolation] = field(default_factory=list)
    runtime_load_sites: list[RuntimeLoadSite] = field(default_factory=list)
    runtime_load_classifications: dict[str, str] = field(default_factory=dict)
    routes: int = 0
    waveform_backend: str = ""
    #: Which analysis backend the PAYLOAD interpreter could actually load, and
    #: whether the analyze-on-import loop would arm. In the manifest because a
    #: verdict that lives only in a build log cannot be re-read off the
    #: artifact a tester is holding.
    analysis_backend: str = ""


# ----- git identity ------------------------------------------------------
def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PayloadBuildError(
            f"git {' '.join(args)} failed in {repo_root}: "
            f"{result.stderr.strip() or result.returncode}. The payload "
            "manifest must be able to state what it was built from; an "
            "artifact that cannot name its own commit is exactly the stale "
            "build this stamp exists to catch."
        )
    return result.stdout.strip()


def git_identity(repo_root: Path) -> dict[str, object]:
    """Stamp what the tree was at build time. Never guesses, never defaults.

    ``git_dirty`` is not decoration. A dirty build is not reproducible from
    its sha, so the artifact says so on its own main surface rather than
    looking identical to a clean one.
    """
    if shutil.which("git") is None:
        raise PayloadBuildError(
            "git is not on PATH; the payload manifest cannot be stamped with "
            "the commit it was built from"
        )
    sha_full = _git(repo_root, "rev-parse", "HEAD")
    return {
        "git_sha": sha_full[:8],
        "git_sha_full": sha_full,
        "git_branch": _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": _git(repo_root, "status", "--porcelain") != "",
    }


# ----- interpreter pin ---------------------------------------------------
PYTHON_PIN_FILE: str = ".python-version"
PYTHON_PIN_PATTERN: re.Pattern[str] = re.compile(r"(?P<minor>\d+\.\d+)(?:\.\d+)?")


def read_python_pin(repo_root: Path) -> str:
    """The repo's pinned interpreter version, verbatim from .python-version.

    The pin is the reproducibility contract: without it the artifact's
    interpreter silently depended on whichever CPython uv resolved on the
    build machine (the Sun 31 Aug 2026 build staged 3.13.3, a fresh worktree
    the next day staged 3.14.7). A missing or unreadable pin fails the build;
    guessing a version here would recreate the machine-dependence the pin
    exists to remove.
    """
    pin_path = repo_root / PYTHON_PIN_FILE
    if not pin_path.is_file():
        raise PayloadBuildError(
            f"{pin_path} does not exist. The payload's interpreter must come "
            "from the repo's pinned line, not from whatever CPython uv "
            "resolves on this machine; restore the pin rather than building "
            "an artifact whose interpreter depends on who built it."
        )
    pin = pin_path.read_text(encoding="utf-8").strip()
    if PYTHON_PIN_PATTERN.fullmatch(pin) is None:
        raise PayloadBuildError(
            f"{pin_path} contains {pin!r}, which is not a plain "
            "major.minor[.patch] version; the pin must name the exact "
            "interpreter line the payload ships."
        )
    return pin


def assert_runtime_matches_pin(
    *, runtime_version: str, pin: str, runtime_source: Path
) -> None:
    """The staged runtime's major.minor must equal the repo pin's.

    major.minor, not the full triple, because that is the granularity at
    which CPython changes behavior the prune list depends on: 3.14 statically
    links libpython where 3.11 links it dynamically, and 3.14's pdb.py gained
    a module-scope ``import _pyrepl.utils`` that makes the _pyrepl prune
    strand the payload (pyrekordbox -> construct -> construct.debug ->
    ``import pdb``, hit live Tue 1 Sep 2026). A patch bump within the pinned
    line carries neither kind of change and refusing it would block every
    security release for no safety gain.
    """
    def _minor(version: str) -> str:
        return ".".join(version.split(".")[:2])

    if _minor(runtime_version) != _minor(pin):
        raise PayloadBuildError(
            f"the staged runtime is Python {runtime_version} but "
            f"{PYTHON_PIN_FILE} pins {pin} (runtime source: {runtime_source}). "
            "The prune list and the payload verification are only proven on "
            "the pinned line, and an artifact's interpreter must not depend "
            "on which machine built it. Rebuild the venv on the pin "
            "(rm -rf .venv && uv sync --extra dev) or pass a matching "
            "--runtime-source."
        )


def _interpreter_version(python: Path) -> str:
    return subprocess.run(
        [str(python), "-c", "import sys; print(sys.version.split()[0])"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# ----- SPA freshness -----------------------------------------------------
def _newest_mtime(paths: list[Path]) -> tuple[float, Path | None]:
    newest = 0.0
    winner: Path | None = None
    for path in paths:
        if not path.exists():
            continue
        candidates = [path] if path.is_file() else list(path.rglob("*"))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            mtime = candidate.stat().st_mtime
            if mtime > newest:
                newest, winner = mtime, candidate
    return newest, winner


def assert_spa_is_fresh(frontend_dir: Path) -> Path:
    """Refuse to package a missing or stale SPA. Never rebuilds it.

    A silent rebuild here would make the artifact's contents depend on
    whatever node_modules happened to be on the build machine, and would hide
    the fact that the operator packaged a tree they had not built. The build
    stops and says which file is newer than the bundle.
    """
    build_dir = frontend_dir / "build"
    if not build_dir.is_dir() or not any(build_dir.iterdir()):
        raise PayloadBuildError(
            f"{build_dir} is missing or empty. Run "
            # Enter the frontend directory first: corepack resolves the
            # packageManager pin from the current directory before invoking pnpm.
            "`cd apps/webui/frontend && pnpm run build` first; this builder "
            "packages the SPA you built, it does not build one for you."
        )
    source_newest, source_file = _newest_mtime(
        [frontend_dir / name for name in SPA_SOURCE_PATHS]
    )
    build_newest, _ = _newest_mtime([build_dir])
    if source_newest > build_newest:
        raise PayloadBuildError(
            f"{source_file} was modified after the SPA was built "
            f"({_stamp(source_newest)} > {_stamp(build_newest)}). Rebuild the "
            "frontend before packaging; shipping a stale bundle is the exact "
            "failure this gate exists for."
        )
    return build_dir


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def sole_stretch_asset(build_dir: Path) -> Path:
    """The Signalsmith worklet must be present exactly once.

    Deck load depends on the worklet being served as an UNTRANSFORMED module
    asset. Zero copies means no deck audio; more than one means the bundler
    emitted a transformed twin and the wrong one can win.
    """
    assets = sorted((build_dir / "_app/immutable/assets").glob("SignalsmithStretch.*.mjs"))
    if len(assets) != 1:
        raise PayloadBuildError(
            f"expected exactly 1 SignalsmithStretch.*.mjs asset in {build_dir}, "
            f"found {len(assets)}: {[a.name for a in assets]}. A payload "
            "without it cannot load a deck."
        )
    return assets[0]


# ----- linked libraries --------------------------------------------------
def parse_otool(output: str, *, is_dylib: bool) -> list[str]:
    """Dependency paths from ``otool -L``, minus a dylib's own install id.

    Universal binaries repeat the list per architecture behind an
    ``(architecture x86_64):`` header, so sections are tracked and the first
    entry of each is dropped for a dylib (that entry is the id, not a
    dependency).
    """
    dependencies: list[str] = []
    at_section_start = False
    for raw in output.splitlines():
        if not raw.startswith("\t"):
            at_section_start = raw.rstrip().endswith(":")
            continue
        entry = raw.strip().split(" (compatibility", 1)[0].strip()
        if at_section_start and is_dylib:
            at_section_start = False
            continue
        at_section_start = False
        dependencies.append(entry)
    return dependencies


def link_violations(relative: str, dependencies: list[str]) -> list[LinkViolation]:
    """Which of a file's dependencies exist only on the build machine."""
    return [
        LinkViolation(path=relative, dependency=dependency)
        for dependency in dependencies
        if not dependency.startswith(ALLOWED_LINK_PREFIXES)
    ]


# `file -b` says "Mach-O 64-bit dynamically linked shared library arm64" for a
# dylib, "Mach-O 64-bit bundle arm64" for a CPython extension module, and
# "Mach-O 64-bit executable arm64" for the interpreter. A universal binary
# repeats a description per slice inline ("Mach-O universal binary with 2
# architectures: [x86_64:Mach-O 64-bit executable x86_64] [...]"), so a
# substring test answers for thin and fat alike. An ar archive reports
# "current ar archive random library" and is excluded, which matters: the
# staged runtime carries real .a files and codesign and otool both refuse them.
MACHO_DESCRIPTION: str = "Mach-O"
# How file(1) opens its error line. It writes this to STDOUT and exits 0, on
# both the BSD and GNU builds, so it is the only signal a classification
# failure gives.
FILE_ERROR_PREFIX: str = "cannot open"
# otool -hv's filetype column, a documented token rather than prose. Only a
# DYLIB carries an id line in otool -L output.
DYLIB_FILETYPE: str = "DYLIB"


def macho_candidates(payload_dir: Path) -> list[Path]:
    """Files worth asking ``file`` about, narrowed the cheap way first.

    An extension module can be 0644, so the extension clause has to stay; the
    interpreter carries no extension, so the executable-bit clause is what
    reaches it. Symlinks are skipped because runtime/bin/python3 points at the
    versioned binary already in the list.

    Mirrors macho_files in scripts/lib/macho.sh on purpose: the set this bar
    inspects and the set the signing scripts sign should be the same set.
    """
    return [
        path
        for path in sorted(payload_dir.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and (path.suffix in (".so", ".dylib") or os.access(path, os.X_OK))
    ]


def describe_macho(path: Path) -> str | None:
    """``file -b`` for a Mach-O file, or None for anything that is not one.

    Detection is by CONTENT. The predicate this replaced was
    ``path.suffix in (".so", ".dylib")``, which never inspected the one binary
    the payload cannot run without: the bundled CPython at
    runtime/bin/python3.N carries no extension, so this bar had never looked
    at it. The signing paths carried the same defect and are fixed the same
    way, in scripts/lib/macho.sh.
    """
    result = subprocess.run(
        ["file", "-b", str(path)], capture_output=True, text=True, check=False
    )
    description = " ".join(result.stdout.split())
    # A tool that cannot measure must report UNKNOWN, never a verdict. Reading
    # a failed classification as "not Mach-O" would drop the file from the
    # scan and let a release report success over something nothing inspected,
    # which is the same shape as the bug this function exists to fix.
    #
    # The exit code alone will NOT tell you (measured on macOS 15 and matching
    # the BSD and GNU manuals): `file` reports a missing or unreadable path by
    # writing "cannot open ..." to STDOUT and exiting 0. A returncode check
    # here would be a guard that never fires.
    if result.returncode != 0 or not description or description.startswith(FILE_ERROR_PREFIX):
        raise PayloadBuildError(
            f"file(1) could not classify {path} (exit {result.returncode}): "
            f"{description or result.stderr.strip() or 'no output'}. The "
            "linked-library bar cannot classify this candidate, and an "
            "unclassified payload file is not shippable."
        )
    return description if MACHO_DESCRIPTION in description else None


def macho_filetype(path: Path) -> str:
    """The Mach-O filetype token from ``otool -hv``: EXECUTE, DYLIB, BUNDLE.

    Whether ``otool -L`` opens with the file's own install name or with a real
    dependency is a question about FILETYPE. Only a DYLIB carries an id line;
    a BUNDLE (every CPython extension module) and an EXECUTABLE (the
    interpreter) do not, and treating one as though it did silently discards
    its FIRST dependency.

    Read from otool rather than from file(1) prose. file(1) answers the
    is-it-Mach-O question portably, but its wording will not carry a filetype:
    macOS says "Mach-O 64-bit executable arm64" where GNU file says "Mach-O
    64-bit arm64 executable, flags:<...>" (observed on ubuntu-latest, Tue 1
    Sep 2026), and GNU file describes a bundle with a phrase that CONTAINS the
    one it uses for a shared library, so a substring test would call every
    bundle a dylib. otool -hv prints a token.

    A universal binary repeats the header per slice; all slices share a
    filetype, and a file whose slices disagree is refused rather than guessed.
    """
    result = subprocess.run(
        ["otool", "-hv", str(path)], capture_output=True, text=True, check=False
    )
    types = {
        line.split()[4]
        for line in result.stdout.splitlines()
        if line.startswith("MH_") and len(line.split()) > 4
    }
    if result.returncode != 0 or len(types) != 1:
        raise PayloadBuildError(
            f"otool -hv could not give {path} a single Mach-O filetype "
            f"(exit {result.returncode}, saw {sorted(types) or 'nothing'}). "
            "Its linked libraries cannot be read correctly without one, and a "
            "guessed filetype drops a real dependency from the scan."
        )
    return types.pop()


def scan_linked_libraries(payload_dir: Path) -> tuple[int, list[LinkViolation]]:
    """otool -L every Mach-O file in the payload, found by content.

    The bar copied from lane A's shipped artifact: zero references to
    /opt/homebrew, /usr/local or any absolute path outside the bundle and the
    OS. A friend's Mac has no Homebrew, so a single such link is a bundle
    that only runs here.

    "Every Mach-O" used to mean "every .so and .dylib", which silently
    excluded the bundled interpreter at runtime/bin/python3.N. See
    describe_macho.

    Each ``otool -L`` is its own subprocess, so the scan is parallelized
    with a thread pool (subprocess wait time releases the GIL). Results are
    gathered keyed by path and re-merged in the original sorted order, so
    the returned violation list is byte-identical to a serial scan
    regardless of which thread finishes first.
    """
    if shutil.which("otool") is None:
        raise PayloadBuildError(
            "otool is not on PATH (install the Xcode command line tools); the "
            "linked-library bar cannot be checked and an unchecked payload is "
            "not shippable"
        )
    if shutil.which("file") is None:
        raise PayloadBuildError(
            "file(1) is not on PATH; Mach-O files cannot be identified by "
            "content and an unchecked payload is not shippable"
        )
    # Two passes, both parallelized: `file` decides what is Mach-O, then
    # otool reads the links of whatever survived. The descriptions are kept
    # rather than discarded because the second pass needs the filetype.
    considered = macho_candidates(payload_dir)
    with ThreadPoolExecutor() as executor:
        kinds = dict(zip(considered, executor.map(describe_macho, considered), strict=True))
    candidates = [path for path in considered if kinds[path] is not None]

    def _scan_one(path: Path) -> list[LinkViolation]:
        result = subprocess.run(
            ["otool", "-L", str(path)], capture_output=True, text=True, check=True
        )
        return link_violations(
            str(path.relative_to(payload_dir)),
            parse_otool(result.stdout, is_dylib=macho_filetype(path) == DYLIB_FILETYPE),
        )

    with ThreadPoolExecutor() as executor:
        results = dict(
            zip(candidates, executor.map(_scan_one, candidates), strict=True)
        )

    violations: list[LinkViolation] = []
    for path in candidates:
        violations.extend(results[path])
    return len(candidates), violations


# ----- runtime-loaded libraries -----------------------------------------
def find_runtime_load_sites(root: Path, payload_dir: Path) -> list[RuntimeLoadSite]:
    """Every ctypes site that resolves a library while the daemon runs.

    otool cannot see these, and a clean environment does not stop them:
    dyld's own search path is not something ``env -i`` clears, so a
    find_library that happens to hit a Homebrew dylib on the build Mac passes
    every static check here and fails on a machine without it.
    """
    sites: list[RuntimeLoadSite] = []
    for path in sorted(root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "find_library" not in text and "CDLL" not in text and "LoadLibrary" not in text:
            continue
        relative = str(path.relative_to(payload_dir))
        for lineno, line in enumerate(text.splitlines(), start=1):
            sites.extend(
                RuntimeLoadSite(
                    path=relative,
                    line=lineno,
                    call=match.group("call"),
                    library=match.group("literal"),
                    source=line.strip(),
                )
                for match in CTYPES_SITE_PATTERN.finditer(line)
            )
    return sites


def classify_runtime_load_sites(
    sites: list[RuntimeLoadSite],
) -> tuple[dict[str, str], list[RuntimeLoadSite]]:
    """Split sites into classified (with a recorded reason) and unclassified.

    Unclassified is a build failure. "We did not look" and "it is fine" must
    never render the same way in a report.
    """
    classified: dict[str, str] = {}
    unclassified: list[RuntimeLoadSite] = []
    for site in sites:
        exemption = next(
            (
                reason
                for suffix, reason in RUNTIME_LOAD_SITE_REASONS
                if site.path.endswith(suffix)
            ),
            None,
        )
        if exemption is not None:
            classified[site.describe()] = exemption
        elif site.key in RUNTIME_LOAD_ALLOWLIST:
            classified[site.describe()] = RUNTIME_LOAD_ALLOWLIST[site.key]
        else:
            unclassified.append(site)
    return classified, unclassified


def stale_line_classifications(
    sites: list[RuntimeLoadSite], payload_dir: Path
) -> list[str]:
    """Line-keyed allowlist entries whose file ships but whose line holds no site.

    A dependency bump moves every dynamic site, and the old ``path:line``
    entry is then a reason attached to whatever the new version put on that
    line. It classifies nothing today, but the next version to land a load
    site there would inherit a justification written for different code, and
    the build would pass it silently. An entry for a file this payload does
    not carry is not stale: that dependency is simply absent from this build.
    """
    live_keys = {site.key for site in sites}
    return sorted(
        key
        for key in RUNTIME_LOAD_ALLOWLIST
        if (match := LINE_KEY_PATTERN.match(key)) is not None
        and (payload_dir / match.group("path")).is_file()
        and key not in live_keys
    )


def scan_runtime_loaded_libraries(
    payload_dir: Path,
) -> tuple[list[RuntimeLoadSite], dict[str, str]]:
    sites = find_runtime_load_sites(payload_dir, payload_dir)
    classified, unclassified = classify_runtime_load_sites(sites)
    stale = stale_line_classifications(sites, payload_dir)
    problems: list[str] = []
    if unclassified:
        listing = "\n  ".join(
            f"{site.describe()}  ->  {site.source}" for site in unclassified
        )
        problems.append(
            "unclassified runtime library loads in the payload; each one is a "
            "library resolved on the tester's machine that otool cannot see. "
            "Bundle it, drop the dependency, or record why it is unreachable "
            f"in RUNTIME_LOAD_ALLOWLIST:\n  {listing}"
        )
    if stale:
        listing = "\n  ".join(stale)
        problems.append(
            "stale RUNTIME_LOAD_ALLOWLIST entries: the file ships but the line "
            "holds no runtime load site any more (a dependency bump moved it). "
            "Re-read the file and re-key or delete each entry, so no future "
            f"site inherits a reason written for other code:\n  {listing}"
        )
    if problems:
        raise PayloadBuildError("\n".join(problems))
    return sites, classified


# ----- digests and sizes -------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    """A digest over relative paths AND contents, so a rename shows up."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def tree_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


# ----- staging -----------------------------------------------------------
def _prune_pycache(root: Path) -> int:
    removed = 0
    for path in sorted(root.rglob("__pycache__"), reverse=True):
        if path.is_dir():
            shutil.rmtree(path)
            removed += 1
    for path in root.rglob("*.pyc"):
        path.unlink()
        removed += 1
    return removed


def stage_runtime(runtime_source: Path, destination: Path) -> list[str]:
    """Copy the relocatable CPython, then delete what a daemon never uses."""
    if not (runtime_source / "bin/python3").exists():
        raise PayloadBuildError(
            f"{runtime_source} does not look like a python-build-standalone "
            "install (no bin/python3)"
        )
    shutil.copytree(runtime_source, destination, symlinks=True)
    pruned: list[str] = []
    for relative in RUNTIME_PRUNE_RELATIVE:
        for target in sorted(destination.glob(relative)):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
                pruned.append(str(target.relative_to(destination)))
            elif target.exists() or target.is_symlink():
                target.unlink()
                pruned.append(str(target.relative_to(destination)))
    _prune_pycache(destination)
    return pruned


@dataclass(frozen=True)
class LockedRequirement:
    """One pinned line of ``uv export``, with the edges that pulled it in."""

    name: str
    spec: str
    via: frozenset[str]


def parse_locked_export(text: str) -> list[LockedRequirement]:
    """Read ``uv export``'s pinned lines AND its ``# via`` reverse edges.

    The via comments are the dependency graph uv already computed. Parsing
    them is what lets an exclusion drop the packages that existed only to
    serve it, instead of leaving orphans in the payload forever.
    """
    entries: list[LockedRequirement] = []
    parents: list[str] = []
    in_via_block = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped == "":
            continue
        if raw.startswith(("#", "-")):
            continue
        if stripped.startswith("#"):
            comment = stripped.lstrip("#").strip()
            if comment == "via":
                in_via_block = True
            elif comment.startswith("via "):
                parents.append(comment[4:].strip())
                in_via_block = False
            elif in_via_block:
                parents.append(comment)
            if entries and parents:
                entries[-1] = LockedRequirement(
                    name=entries[-1].name,
                    spec=entries[-1].spec,
                    via=frozenset(parents),
                )
            continue
        parents = []
        in_via_block = False
        entries.append(
            LockedRequirement(
                name=_requirement_name(stripped),
                spec=stripped,
                via=frozenset(),
            )
        )
    return entries


def prune_excluded(
    entries: list[LockedRequirement], project_name: str
) -> tuple[list[LockedRequirement], list[str]]:
    """Drop the classified exclusions and everything only they needed.

    A package survives if any surviving parent still names it, or if the
    project itself declares it. Iterating to a fixed point matters: dropping
    pyacoustid orphans audioread, and dropping audioread orphans the three
    standard-* shims behind it.
    """
    dropped = {
        entry.name for entry in entries if entry.name in EXCLUDED_DEPENDENCIES
    }
    unmatched = set(EXCLUDED_DEPENDENCIES) - dropped
    if unmatched:
        raise PayloadBuildError(
            f"EXCLUDED_DEPENDENCIES names {sorted(unmatched)}, which the lock "
            "does not contain any more. A stale exclusion is a claim nobody "
            "re-checked; delete it or fix the name."
        )
    changed = True
    while changed:
        changed = False
        for entry in entries:
            if entry.name in dropped or not entry.via:
                continue
            if entry.via <= dropped and project_name not in entry.via:
                dropped.add(entry.name)
                changed = True
    kept = [entry for entry in entries if entry.name not in dropped]
    return kept, sorted(dropped)


def _verify_omitted_extras(
    entries: list[LockedRequirement], pyproject: dict[str, Any]
) -> None:
    """The build's re-check for :data:`OMITTED_OPTIONAL_EXTRAS`.

    Mirrors ``prune_excluded``'s self-check but in the opposite direction: an
    exclusion there claims a package IS in the closure and gets dropped; an
    omission here claims a whole [extras] group is NOT in the closure because
    no extra was requested. Two ways that claim can go stale, both fatal:
    pyproject.toml grows or renames an extra this registry never audits, or a
    package belonging to an audited extra shows up in the export anyway (a
    stray direct dependency, an extra added elsewhere without updating this
    record). Either is exactly the unaudited-omission failure issue #795
    found for "tags"/mutagen, generalised to every extra the export omits.

    A package listed under an audited extra can ALSO be a core (hard)
    dependency -- numpy is both a core dep (the Rekordbox waveform path) and
    part of "analysis" (the madmom-compatible numpy<2 pin). Core deps are in
    the locked export no matter which extras were requested, so their
    presence proves nothing about the extra leaking in; excluding them keeps
    the check meaningful for the extra's actually-distinguishing packages.

    The same holds one level down: a package an extra names can also be a
    transitive dependency of a core dep -- httpx2 is in "dev" for the test
    client AND required by mcp 2.x, a core dep since PR #2806. The export's
    ``# via`` edges tell the two apart. A requested extra is pulled in by the
    project itself, so only a package with the project among its ``via``
    parents can be that extra leaking; one reached solely through other
    packages is part of the core closure.
    """
    declared_extras: dict[str, list[str]] = pyproject["project"][
        "optional-dependencies"
    ]
    audited = set(OMITTED_OPTIONAL_EXTRAS)
    requested = set(REQUESTED_OPTIONAL_EXTRAS)
    both = audited & requested
    if both:
        raise PayloadBuildError(
            f"extras {sorted(both)} are registered as BOTH omitted and "
            "requested, so the two registries disagree about what this "
            "build ships. One of them is wrong; the check cannot pick."
        )
    unaudited = (set(declared_extras) - {"all"}) - audited - requested
    if unaudited:
        raise PayloadBuildError(
            f"pyproject.toml defines extras {sorted(unaudited)} that neither "
            "OMITTED_OPTIONAL_EXTRAS nor REQUESTED_OPTIONAL_EXTRAS names -- "
            "record each one with its reason or the build stops proving what "
            "it ships and what it silently leaves out."
        )
    stale = (audited | requested) - set(declared_extras)
    if stale:
        raise PayloadBuildError(
            f"the extras registries name {sorted(stale)}, which "
            "pyproject.toml no longer defines as an [extras] group. A stale "
            "entry is a claim nobody re-checked; delete it or fix the name."
        )
    project_name = pyproject["project"]["name"]
    locked_names = {entry.name for entry in entries if project_name in entry.via}
    core_dep_names = {
        _requirement_name(req) for req in pyproject["project"]["dependencies"]
    }
    # PRESENCE half. A requested extra whose distinguishing packages are not
    # in the closure is the failure this whole change exists to stop
    # recurring: the export quietly stops carrying librosa and the installed
    # app goes back to analyzing nothing, with no build-time signal at all.
    for extra in sorted(requested):
        extra_names = {
            _requirement_name(req) for req in declared_extras[extra]
        } - core_dep_names
        absent = extra_names - locked_names
        if absent:
            raise PayloadBuildError(
                f"the {extra!r} extra is requested by this build but its "
                f"packages {sorted(absent)} are NOT in the locked export "
                "as project-pulled requirements. Shipping the extra is what "
                "makes the feature it backs work at all, so a silent "
                "drop-out fails the build rather than the tester."
            )
    for extra in sorted(audited):
        extra_names = {
            _requirement_name(req) for req in declared_extras[extra]
        } - core_dep_names
        present = locked_names & extra_names
        if present:
            raise PayloadBuildError(
                f"the {extra!r} extra's packages {sorted(present)} are in "
                "the locked export even though this build never requests "
                "that extra -- update OMITTED_OPTIONAL_EXTRAS or fix the "
                "export; shipping them silently is the exact "
                "unaudited-omission failure issue #795 found."
            )


def locked_requirements(repo_root: Path) -> tuple[list[LockedRequirement], list[str]]:
    """The LOCKED closure, not a fresh resolve.

    Learned the expensive way on the first build of this payload: resolving
    from the pyproject ranges pulled fastapi 0.141 / starlette 1.6 against a
    tree developed and tested on 0.136 / 1.2, and every ``include_router``
    call silently registered nothing. The engine booted, answered /health,
    and served an app with six routes. A payload must ship the versions the
    test suite ran against, so the lock is the input.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise PayloadBuildError("uv is not on PATH; cannot export the payload deps")
    result = subprocess.run(
        [
            uv,
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements-txt",
            *[arg for extra in sorted(REQUESTED_OPTIONAL_EXTRAS) for arg in ("--extra", extra)],
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PayloadBuildError(f"uv export failed:\n{result.stderr}")
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    entries = parse_locked_export(result.stdout)
    _assert_never_ship_absent(entries)
    _verify_omitted_extras(entries, pyproject)
    return prune_excluded(entries, pyproject["project"]["name"])


def _requirement_name(requirement: str) -> str:
    return re.split(r"[\[<>=!;~ ]", requirement.strip(), maxsplit=1)[0].lower()


def install_dependencies(
    locked: list[LockedRequirement], python: Path, destination: Path
) -> None:
    """uv installs the closure against the PAYLOAD interpreter, not the venv.

    ``--python`` is what makes the environment markers and the wheel tags
    match the interpreter that will actually import them; resolving against
    the build venv would happily install a wheel the payload cannot load.

    ``--no-deps`` is what makes :data:`EXCLUDED_DEPENDENCIES` real for a
    package a KEPT package still declares. The export is already the whole
    locked closure, so there is nothing left for uv to resolve; without the
    flag it re-resolves each pruned name out of the survivors' wheel metadata
    and installs whatever the index has today (cryptography's ``cffi>=2.0.0``
    quietly re-added cffi 2.1.1 against a lock pinning 2.0.0, Fri 11 Sep
    2026). :func:`assert_installed_is_locked` then proves the flag did its
    job rather than trusting it.
    """
    if shutil.which("uv") is None:
        raise PayloadBuildError("uv is not on PATH; cannot install the payload deps")
    destination.mkdir(parents=True, exist_ok=True)
    requirements_file = destination.parent / "payload-requirements.txt"
    requirements_file.write_text(
        "\n".join(entry.spec for entry in locked) + "\n", encoding="utf-8"
    )
    result = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--no-deps",
            "--python",
            str(python),
            "--target",
            str(destination),
            "--requirement",
            str(requirements_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    requirements_file.unlink()
    if result.returncode != 0:
        raise PayloadBuildError(
            f"uv pip install failed:\n{result.stdout}\n{result.stderr}"
        )
    assert_installed_is_locked(destination, locked)
    _prune_pycache(destination)
    _prune_dependency_test_suites(destination)


def _normalize_distribution_name(name: str) -> str:
    """PEP 503 normalization, so ``annotated_doc`` and ``annotated-doc`` agree."""
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_distributions(pylib: Path) -> dict[str, str]:
    """``{normalized name: version}`` for every ``*.dist-info`` under pylib."""
    found: dict[str, str] = {}
    for path in sorted(pylib.glob("*.dist-info")):
        name, _, version = path.name[: -len(".dist-info")].rpartition("-")
        found[_normalize_distribution_name(name)] = version
    return found


def assert_installed_is_locked(pylib: Path, locked: list[LockedRequirement]) -> None:
    """Every distribution in pylib must be one the lock pins, at that version.

    Presence, not absence: each installed package has to be positively
    matched to a ``name==version`` line of the export. A package the lock
    never named, or one at a version the lock does not pin, is the payload
    shipping something nobody reviewed. A locked package that is absent is
    fine here: environment markers skip some on purpose, and the import +
    route-table boot is the check for a missing one.
    """
    # A universal export can pin one name at several versions under disjoint
    # markers (networkx by python_full_version, torch "+cpu" on linux), so a
    # name maps to the SET of versions some locked line pins it at.
    pinned: dict[str, set[str]] = {}
    for entry in locked:
        match = re.search(r"==([^\s;]+)", entry.spec)
        if match is None:
            raise PayloadBuildError(f"locked line is not pinned with ==: {entry.spec!r}")
        pinned.setdefault(_normalize_distribution_name(entry.name), set()).add(match.group(1))
    strays = sorted(
        f"{name}=={version}"
        for name, version in installed_distributions(pylib).items()
        if version not in pinned.get(name, set())
    )
    if strays:
        raise PayloadBuildError(
            "pylib contains distributions the lock does not pin at that version, "
            "so uv resolved past the locked closure (a pruned package re-added "
            f"through a survivor's metadata, or --no-deps lost): {strays}"
        )


def _prune_dependency_test_suites(pylib: Path) -> int:
    removed = 0
    for name in PYLIB_PRUNE_DIR_NAMES:
        for path in sorted(pylib.rglob(name), reverse=True):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                removed += 1
    return removed


def sole_waveform_wheel(wheel_dir: Path) -> Path:
    """Exactly one wheel, or the build stops.

    A stale --out directory must never let the build pick a wheel at random;
    zero wheels means maturin claimed success while producing nothing.
    """
    wheels = sorted(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        listing = ", ".join(w.name for w in wheels) or "<none>"
        raise PayloadBuildError(
            f"expected exactly one waveform wheel in {wheel_dir}, found: {listing}"
        )
    return wheels[0]


def build_waveform_native_wheel(repo_root: Path, wheel_dir: Path) -> Path:
    """Release-profile maturin wheel for ``_rb_waveform_native``.

    The wheel then has to pass the same fail-closed tag/content gate the
    official project wheel passes (scripts/check_waveform_native_wheel.py):
    a debug build or a wheel missing the extension must stop the build here,
    never ship as a silent NumPy fallback.
    """
    if shutil.which("uv") is None:
        raise PayloadBuildError("uv is not on PATH; cannot build the waveform wheel")
    result = subprocess.run(
        [
            "uv",
            "tool",
            "run",
            "--from",
            "maturin>=1.8,<2",
            "maturin",
            "build",
            "--release",
            "--manifest-path",
            str(repo_root / WAVEFORM_CRATE_MANIFEST),
            "--out",
            str(wheel_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PayloadBuildError(
            f"maturin build of the waveform extension failed:\n{result.stderr}"
        )
    wheel = sole_waveform_wheel(wheel_dir)
    check = subprocess.run(
        [sys.executable, str(repo_root / WAVEFORM_WHEEL_CHECKER), str(wheel)],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode != 0:
        raise PayloadBuildError(
            f"waveform wheel failed the release gate:\n{check.stdout}\n{check.stderr}"
        )
    return wheel


def stage_audio_engine(repo_root: Path, payload_dir: Path) -> dict[str, object]:
    """Build ``odj-audio`` with device output and stage it at ``bin/odj-audio``.

    Release profile, ``--locked``, ``--features device``: the one build that can
    play to the Mac's output. The staged binary must then say so itself: its
    hello has to report protocol 1 and ``device: true``. A build without the
    feature would boot, pass every other check and fail only when a DJ turned
    the Rust engine on.
    """
    if shutil.which("cargo") is None:
        raise PayloadBuildError("cargo is not on PATH; cannot build the audio engine")
    crate = repo_root / AUDIO_ENGINE_CRATE
    target_dir = crate / "target"
    result = subprocess.run(
        [
            "cargo",
            "build",
            "--release",
            "--locked",
            "--features",
            "device",
            "--bin",
            "odj-audio",
            "--manifest-path",
            str(crate / "Cargo.toml"),
            "--target-dir",
            str(target_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PayloadBuildError(f"cargo build of odj-audio failed:\n{result.stderr}")
    staged = payload_dir / AUDIO_ENGINE_RELATIVE
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target_dir / "release/odj-audio", staged)
    staged.chmod(0o755)
    return verify_audio_engine(staged)


def verify_audio_engine(binary: Path) -> dict[str, object]:
    """Boot the staged engine on its fake clock and read its hello.

    Closed stdin stops the engine right after the hello, so this needs no
    output device on the build machine.
    """
    try:
        result = subprocess.run(
            [str(binary), "serve", "--clock", "fake"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PayloadBuildError(f"{binary} did not stop within 30 s of stdin closing") from exc
    first = result.stdout.splitlines()[0] if result.stdout else ""
    try:
        hello = json.loads(first)
    except json.JSONDecodeError as exc:
        raise PayloadBuildError(
            f"{binary} printed no hello (exit {result.returncode}): "
            f"stdout {result.stdout[:200]!r} stderr {result.stderr[:500]!r}"
        ) from exc
    if not isinstance(hello, dict) or hello.get("type") != "hello" or hello.get("protocol") != 1:
        raise PayloadBuildError(f"{binary} answered {first!r}, not a protocol 1 hello")
    if hello.get("device") is not True:
        raise PayloadBuildError(
            f"{binary} reports device={hello.get('device')!r}; the payload needs "
            "the --features device build"
        )
    return hello


def install_waveform_native(
    wheel: Path, python: Path, destination: Path, requirements: list[str]
) -> None:
    """Install the extension wheel into pylib, runtime deps from the closure.

    ``--no-deps`` is load-bearing: a ``--target`` install cannot see what is
    already in the target, so resolving the wheel's own ``numpy>=1.26`` would
    install a SECOND numpy on top of the locked closure's pin. The closure
    must therefore already carry numpy, and that is checked, not assumed.
    """
    if not any(_requirement_name(spec) == "numpy" for spec in requirements):
        raise PayloadBuildError(
            "the locked closure no longer carries numpy; _rb_waveform_native "
            "needs it at runtime and --no-deps will not install it"
        )
    result = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--target",
            str(destination),
            "--no-deps",
            str(wheel),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PayloadBuildError(
            f"uv pip install of the waveform wheel failed:\n{result.stdout}\n{result.stderr}"
        )


def skip_output_tree(output_root: Path) -> Callable[[str, list[str]], set[str]]:
    """Copy filter that refuses to copy the payload into itself.

    The staging directory is ``apps/desktop/src-tauri/payload``, the path
    ``tauri.conf.json`` names, and ``apps`` is exactly what this build copies.
    Without this the walk descends into its own output until the filesystem
    refuses the path length. Matching the RESOLVED path rather than the name
    is deliberate: ``ignore_patterns("payload")`` would also silently drop any
    unrelated directory a future engine module happens to call payload.
    """
    resolved = output_root.resolve()
    named = shutil.ignore_patterns(
        "__pycache__",
        "*.pyc",
        "node_modules",
        "target",
        "test-results",
        ".svelte-kit",
    )

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = set(named(directory, names))
        here = Path(directory)
        skipped.update(name for name in names if (here / name).resolve() == resolved)
        return skipped

    return ignore


def stage_app_source(repo_root: Path, destination: Path, build_dir: Path) -> None:
    """Copy the engine source, replacing the frontend tree with its build.

    The frontend source, its node_modules and its tests are ~600MB of things
    no daemon reads. What the engine serves is build/, and the engine finds
    it at ``apps/webui/frontend/build`` relative to its own __file__, so the
    directory shape is preserved exactly and only its contents narrowed.
    """
    destination.mkdir(parents=True, exist_ok=True)
    frontend_relative = Path("apps/webui/frontend")
    ignore = skip_output_tree(destination.parent)
    for name in APP_SOURCE_ROOTS:
        shutil.copytree(
            repo_root / name,
            destination / name,
            symlinks=False,
            ignore=ignore,
        )
    staged_frontend = destination / frontend_relative
    if staged_frontend.exists():
        shutil.rmtree(staged_frontend)
    staged_frontend.mkdir(parents=True)
    shutil.copytree(build_dir, staged_frontend / "build", symlinks=False)
    _prune_pycache(destination)
    _stage_glossary_synonym_map(repo_root, destination)
    _stage_vocal_worker(repo_root, destination)
    _stage_stems_workers(repo_root, destination)


def _stage_glossary_synonym_map(repo_root: Path, destination: Path) -> None:
    """Copy META-06 synonym map into the payload app root.

    ``apps.open_dj.glossary.MAP_PATH`` resolves ``parents[2] / open-dj /
    synonym-map.json`` from ``payload/app/apps/open_dj/glossary.py``, so the
    data file must live at ``payload/app/open-dj/synonym-map.json``.
    """
    src = repo_root / GLOSSARY_MAP_REPO_RELATIVE
    if not src.is_file():
        raise PayloadBuildError(f"glossary synonym map missing at {src}")
    dst = destination / GLOSSARY_MAP_PAYLOAD_RELATIVE
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _stage_vocal_worker(repo_root: Path, destination: Path) -> None:
    """Copy the local Demucs worker into the self-contained payload."""
    src = repo_root / VOCAL_WORKER_REPO_RELATIVE
    if not src.is_file():
        raise PayloadBuildError(f"vocal worker missing at {src}")
    dst = destination / VOCAL_WORKER_PAYLOAD_RELATIVE
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _stage_stems_workers(repo_root: Path, destination: Path) -> None:
    """Copy every stems/cloud job worker script into ``payload/app/scripts``.

    The engine spawns these by absolute path under ``PROJECT_ROOT``, which is
    ``payload/app`` here. Without them a user who installed the dmg could not
    produce stems by any route (issue #3421): ``APP_SOURCE_ROOTS`` is
    ``apps`` alone, and every stems producer is a ``scripts/`` file. The list
    has ONE home, ``apps.stems.worker_launch.PAYLOAD_WORKER_SCRIPTS``, so the
    spawn side and the staging side cannot name different files.
    """
    from apps.stems.worker_launch import PAYLOAD_WORKER_SCRIPTS

    for relative in PAYLOAD_WORKER_SCRIPTS:
        src = repo_root / relative
        if not src.is_file():
            raise PayloadBuildError(f"stems worker missing at {src}")
        dst = destination / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


PAYLOAD_LAUNCHER_PREAMBLE: str = """
set -eu

# `opendj install-cli` puts a symlink to this launcher on PATH, so $0 can be
# that link (or a chain of them, absolute or relative). Resolve it to the real
# file first; derived from the link, every path below would point at the link's
# directory instead of the payload.
self=$0
while [ -L "$self" ]; do
    link=$(readlink -- "$self")
    case $link in
        /*) self=$link ;;
        *) self=$(dirname -- "$self")/$link ;;
    esac
done
here=$(cd -- "$(dirname -- "$self")" && pwd)
payload=$(cd -- "$here/.." && pwd)

# One-way safety is absolute. Rekordbox writeback is opt-in through this
# variable, and an installed app must never be the thing that opts in, so the
# launcher UNSETS it rather than trusting that no caller exported it.
unset MDT_REKORDBOX_WRITEBACK_ENABLED

# The engine reads its own identity from here; absent means "repo checkout".
OPENDJ_PAYLOAD_MANIFEST="$payload/manifest.json"
export OPENDJ_PAYLOAD_MANIFEST

# app/ first so the engine's own modules win over anything in the dependency
# tree with the same name.
PYTHONPATH="$payload/app:$payload/pylib"
export PYTHONPATH
# The payload lives inside a read-only .app; writing .pyc next to the source
# would fail on every import.
PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE
# A tester's ~/Library/Python site-packages must never shadow the payload.
PYTHONNOUSERSITE=1
export PYTHONNOUSERSITE
# python -m prepends the CALLER's cwd to sys.path. Launched with a repo
# checkout as cwd, repo code can shadow the payload and mask its actual
# modules. Keep the caller's cwd out of the interpreter search path.
PYTHONSAFEPATH=1
export PYTHONSAFEPATH

# Path to the Desktop-app Google client baked at payload build time.
# Path only; the engine loads the file. Values never appear here.
OPENDJ_GOOGLE_OAUTH_CONFIG="$payload/app/apps/shared/bundled_google_oauth.json"
export OPENDJ_GOOGLE_OAUTH_CONFIG

# Local vocal separation uses the same relocatable runtime and a worker script
# shipped in this payload. The development checkout still uses uv run because
# heavy ML dependencies never enter the repo venv.
MDT_VOCAL_WORKER_PYTHON="$payload/runtime/bin/python3"
MDT_VOCAL_WORKER_SCRIPT="$payload/app/scripts/vocal_region_worker.py"
export MDT_VOCAL_WORKER_PYTHON MDT_VOCAL_WORKER_SCRIPT

# Stems workers (issue #3421) run on the same interpreter and pylib, from
# $payload/app/scripts; apps/stems/worker_launch.py reads this so no stems
# route needs a uv the app does not ship.
MDT_STEM_WORKER_PYTHON="$payload/runtime/bin/python3"
export MDT_STEM_WORKER_PYTHON

# The Rust audio engine the Python engine supervises (plan 20-02). The shell
# may pass the same path; this makes a payload launched any other way (the
# Tauri shell, the CLI) find the build it shipped, never a repo build.
ODJ_AUDIO_BIN="$payload/bin/odj-audio"
export ODJ_AUDIO_BIN

# The beatgrid backfill producer (NATIVE-10) runs Beat This! in its OWN pinned
# site on the checkpoint bundled below, with no uv and no network.
# apps/analysis/backends/own_beatgrid.py refuses to guess either path.
MDT_BEATGRID_RUNNER_PYTHON="$payload/bin/opendj-beatgrid-python"
MDT_BEATGRID_WEIGHTS="$payload/models/beatgrid/beat_this-final0.ckpt"
export MDT_BEATGRID_RUNNER_PYTHON MDT_BEATGRID_WEIGHTS

# The ship Sentry DSN baked at payload build time (OBS-04). Path only; the
# engine loads the file and decides (a packaged build defaults ON, and
# `telemetry-opt-out` in the data directory turns it off).
OPENDJ_BUNDLED_TELEMETRY="$payload/telemetry.json"
export OPENDJ_BUNDLED_TELEMETRY

"""

ENGINE_LAUNCHER_TEMPLATE: str = (
    """#!/bin/sh
# Open DJ engine launcher. THE contract between the desktop shell and the
# payload: the shell knows this path and these two flags, nothing else.
#
# set -eu, never a fallback: every line below either works or stops the
# daemon before it can serve a half-configured library.
"""
    + PAYLOAD_LAUNCHER_PREAMBLE
    + 'exec "$payload/runtime/bin/python3" -m apps.engine_core serve "$@"\n'
)

CLI_LAUNCHER_TEMPLATE: str = (
    """#!/bin/sh
# Open DJ CLI + MCP launcher. Same environment contract as opendj-engine; runs
# apps.opendj_cli so agents can drive the app (including MCP) without a repo
# checkout.
"""
    + PAYLOAD_LAUNCHER_PREAMBLE
    + 'exec "$payload/runtime/bin/python3" -m apps.opendj_cli "$@"\n'
)

# Runs any payload module (`opendj-python -m apps.analysis.queue_cli run ...`)
# under the engine's env contract. The v1 backfill drain is a CLI by design
# (routes/analysis_backfill.py), so an installed app needs this to drain one.
PYTHON_LAUNCHER_TEMPLATE: str = (
    """#!/bin/sh
# Open DJ payload interpreter launcher. Same environment contract as
# opendj-engine; runs whatever module or script it is handed.
"""
    + PAYLOAD_LAUNCHER_PREAMBLE
    + 'exec "$payload/runtime/bin/python3" "$@"\n'
)

# Backward-compatible alias for tests that import LAUNCHER_TEMPLATE.
LAUNCHER_TEMPLATE: str = ENGINE_LAUNCHER_TEMPLATE


def write_engine_launcher(payload_dir: Path) -> Path:
    return write_payload_launcher(payload_dir, LAUNCHER_RELATIVE, ENGINE_LAUNCHER_TEMPLATE)


def write_cli_launcher(payload_dir: Path) -> Path:
    return write_payload_launcher(payload_dir, CLI_LAUNCHER_RELATIVE, CLI_LAUNCHER_TEMPLATE)


def write_python_launcher(payload_dir: Path) -> Path:
    return write_payload_launcher(
        payload_dir, PYTHON_LAUNCHER_RELATIVE, PYTHON_LAUNCHER_TEMPLATE
    )


def write_launchers(payload_dir: Path) -> tuple[Path, Path, Path]:
    return (
        write_engine_launcher(payload_dir),
        write_cli_launcher(payload_dir),
        write_python_launcher(payload_dir),
    )


def write_launcher(payload_dir: Path) -> Path:
    engine_launcher, _cli_launcher, _python_launcher = write_launchers(payload_dir)
    return engine_launcher


# ----- verification ------------------------------------------------------
VERIFY_SNIPPET: str = """
import json, sys, tempfile
from pathlib import Path
from apps.engine_core.config import apply_env_contract, build_config, prepare_layout
data_dir = tempfile.mkdtemp(prefix='opendj-payload-verify-')
cfg = build_config(data_dir, '127.0.0.1', 1)
apply_env_contract(cfg)
prepare_layout(cfg)
from apps.engine_core.app import create_app
from apps.webui.server.app import FRONTEND_BUILD_DIR
from apps.open_dj.glossary import load_synonym_map
from apps.analysis_waveform.native import waveform_materialization_status
load_synonym_map()
rust_bindings_error = None
try:
    import cryptography.hazmat.bindings._rust  # noqa: F401  # imports _cffi_backend at init
except Exception as exc:  # any failure here is the finding, not an error to hide
    rust_bindings_error = f'{type(exc).__name__}: {exc}'
import jwt.algorithms
sentry_sdk_error = None
sentry_sdk_version = None
try:
    import sentry_sdk
    sentry_sdk_version = sentry_sdk.VERSION
except Exception as exc:  # the finding, not something to hide
    sentry_sdk_error = f'{type(exc).__name__}: {exc}'
from apps.analysis import backends as analysis_backends
from apps.webui.server import analysis_autostart
analysis_backend_error = None
try:
    analysis_backends.get_backend(analysis_backends.DEFAULT_BACKEND)
except Exception as exc:  # the finding, not something to hide
    analysis_backend_error = f'{type(exc).__name__}: {exc}'
app = create_app(cfg)
paths = sorted({getattr(r, 'path', '') for r in app.routes})
missing = [p for p in ('/api/v1/health', '/api/v1/tracks', '/api/v1/build-info') if p not in paths]
print(json.dumps({
    'routes': len(paths),
    'missing': missing,
    'frontend_build_dir': str(FRONTEND_BUILD_DIR),
    'frontend_build_exists': FRONTEND_BUILD_DIR.is_dir(),
    'waveform': waveform_materialization_status(),
    'crypto': {
        'has_crypto': bool(jwt.algorithms.has_crypto),
        'rust_bindings_error': rust_bindings_error,
    },
    'telemetry': {
        'sdk_version': sentry_sdk_version,
        'sdk_import_error': sentry_sdk_error,
    },
    'analysis': {
        'backend': analysis_backends.DEFAULT_BACKEND,
        'modules': list(analysis_backends.DEFAULT_BACKEND_MODULES),
        'installed': analysis_backends.default_backend_installed(),
        'import_error': analysis_backend_error,
        'auto_analyze_arms': analysis_autostart.arm_from_environ(
            {analysis_autostart.AUTO_ANALYZE_ENV: 'on'}
        ),
    },
    'python': sys.version.split()[0],
}))
"""


def assert_verify_report(report: dict[str, object]) -> None:
    """Fail the build on any broken claim in the payload's own self-report."""
    if report["missing"]:
        raise PayloadBuildError(
            f"the payload's engine is missing routes {report['missing']}"
        )
    if not report["frontend_build_exists"]:
        raise PayloadBuildError(
            "the payload engine resolves FRONTEND_BUILD_DIR to "
            f"{report['frontend_build_dir']}, which does not exist inside the "
            "payload; the SPA would not be served"
        )
    waveform = report["waveform"]
    assert isinstance(waveform, dict)
    if not waveform["native_available"]:
        # Fail closed, matching the official-wheel gate: a payload must never
        # ship the silent NumPy fallback the ``auto`` policy would pick.
        raise PayloadBuildError(
            "the payload selected the NumPy waveform fallback instead of the "
            f"native extension: {waveform['native_import_error']}"
        )
    analysis = report["analysis"]
    assert isinstance(analysis, dict)
    # The presence of the good thing, asked of the payload's OWN interpreter
    # rather than of the export that fed it. find_spec answering yes and the
    # backend class actually importing are different questions, and it is the
    # second one the analyze-on-import drain asks at run time.
    if not analysis["installed"] or analysis["import_error"] is not None:
        raise PayloadBuildError(
            f"the payload cannot load its {analysis['backend']!r} analysis "
            f"backend (needs {', '.join(analysis['modules'])}), so every "
            "folder import would land with no BPM, no key and no beatgrid "
            f"and no way to get them: {analysis['import_error']}"
        )
    if not analysis["auto_analyze_arms"]:
        raise PayloadBuildError(
            "the payload's analyze-on-import loop declines to arm even with "
            "MUSIC_DJ_AUTO_ANALYZE=on, so an imported track would sit in the "
            "queue forever; see analysis_autostart.arm_from_environ"
        )
    _assert_crypto_loads(report["crypto"])
    _assert_telemetry_sdk_imports(report["telemetry"])


def _assert_crypto_loads(crypto: object) -> None:
    assert isinstance(crypto, dict)
    if crypto["rust_bindings_error"] is not None or not crypto["has_crypto"]:
        # pyjwt swallows cryptography's ImportError and sets has_crypto=False,
        # so a route-table boot passes while RS256 verification is dead at
        # enrollment time. Fri 11 Sep 2026: pruning cffi produced exactly
        # this (cryptography's _rust.abi3.so imports _cffi_backend at init).
        raise PayloadBuildError(
            "the payload's cryptography bindings do not load, so pyjwt reports "
            f"has_crypto={crypto['has_crypto']} and google_id_token RS256 "
            f"verification would fail on a tester's Mac: "
            f"{crypto['rust_bindings_error']}"
        )


def _assert_telemetry_sdk_imports(telemetry: object) -> None:
    assert isinstance(telemetry, dict)
    if telemetry["sdk_import_error"] is not None or not telemetry["sdk_version"]:
        # The presence of the good thing, asked of the payload's OWN
        # interpreter: the export can carry the pin and the install can still
        # have dropped it. Without the SDK the bundled DSN is decoration.
        raise PayloadBuildError(
            "the payload cannot import sentry_sdk, so the bundled DSN would "
            "never be used and no installed engine would report an error "
            f"(OBS-04): {telemetry['sdk_import_error']}"
        )


_PTH_IMPORT_LINE: re.Pattern[str] = re.compile(
    r"^\s*import\s+([a-zA-Z_][\w.]*)"
)
_PTH_DUNDER_IMPORT: re.Pattern[str] = re.compile(
    r"__import__\(\s*['\"]([^'\"]+)['\"]"
)


def parse_pth_imports(pth_path: Path) -> list[str]:
    """Return deduplicated module names referenced by import lines in a .pth."""
    seen: set[str] = set()
    modules: list[str] = []
    for raw_line in pth_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for pattern in (_PTH_IMPORT_LINE, _PTH_DUNDER_IMPORT):
            match = pattern.search(line)
            if match is None:
                continue
            name = match.group(1).split(".")[0]
            if name not in seen:
                seen.add(name)
                modules.append(name)
    return modules


def _payload_interpreter_env(home: str, payload_dir: Path) -> dict[str, str]:
    """Sandbox env for payload interpreter checks (no repo on sys.path)."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "PYTHONPATH": f"{payload_dir / 'app'}:{payload_dir / 'pylib'}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    }


def verify_python_startup_stderr(payload_dir: Path) -> None:
    """Refuse a payload whose interpreter writes to stderr on a no-op start."""
    interpreter = payload_dir / "runtime/bin/python3"
    with tempfile.TemporaryDirectory(prefix="opendj-payload-verify-") as sandbox:
        result = subprocess.run(
            [str(interpreter), "-c", "pass"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            check=False,
            env=_payload_interpreter_env(sandbox, payload_dir),
        )
    stderr = result.stderr.rstrip("\n")
    if result.returncode != 0 or stderr:
        raise PayloadBuildError(
            "the payload interpreter wrote to stderr on startup "
            f"(exit {result.returncode}):\n{stderr}"
        )


def verify_pth_imports(payload_dir: Path) -> None:
    """Refuse a payload whose staged .pth files import absent modules."""
    runtime_dir = payload_dir / "runtime"
    interpreter = payload_dir / "runtime/bin/python3"
    pth_files = sorted(runtime_dir.rglob("*.pth"))
    if not pth_files:
        return
    with tempfile.TemporaryDirectory(prefix="opendj-payload-verify-") as sandbox:
        env = _payload_interpreter_env(sandbox, payload_dir)
        for pth_path in pth_files:
            for module in parse_pth_imports(pth_path):
                result = subprocess.run(
                    [str(interpreter), "-c", f"import {module}"],
                    cwd=sandbox,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=env,
                )
                rel = pth_path.relative_to(payload_dir)
                if result.returncode != 0 or module in result.stderr:
                    raise PayloadBuildError(
                        f"the payload .pth {rel} imports {module!r}, which "
                        "does not resolve inside the payload:\n"
                        f"{result.stderr.strip()}"
                    )


def verify_imports(payload_dir: Path) -> dict[str, object]:
    """Boot the whole route table under the PAYLOAD interpreter.

    This is the check PyInstaller's static analysis only approximates: a
    dependency the closure is missing fails HERE, on the build machine, with
    a traceback, instead of on a tester's Mac with a blank window.

    The subprocess runs with an empty environment plus PATH, so a variable
    that happens to be exported in this shell cannot make the payload look
    self-sufficient when it is not. It also runs from a directory OUTSIDE the
    checkout, because ``python -c`` puts the cwd on sys.path: run it from the
    repo and the repo's own ``apps`` package answers every import, which is
    precisely the thing being tested for absence.
    """
    with tempfile.TemporaryDirectory(prefix="opendj-payload-verify-") as sandbox:
        result = subprocess.run(
            [str(payload_dir / "runtime/bin/python3"), "-c", VERIFY_SNIPPET],
            cwd=sandbox,
            capture_output=True,
            text=True,
            check=False,
            env=_payload_interpreter_env(sandbox, payload_dir),
        )
    if result.returncode != 0:
        raise PayloadBuildError(
            "the payload cannot build its own route table:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert_verify_report(report)
    return report


def assert_no_nested_installers(payload_dir: Path) -> None:
    """No shipped installer may carry another installer inside itself.

    Real incident on the sibling lane: a payload shipped a withdrawn
    installer nested inside itself. A .dmg/.pkg/.app anywhere under the
    staged payload is a packaging mistake, not a legitimate runtime
    dependency, so its mere presence fails the build.
    """
    offenders = sorted(
        {
            path
            for pattern in ("*.dmg", "*.pkg", "*.app")
            for path in payload_dir.rglob(pattern)
        }
    )
    if offenders:
        listing = "\n  ".join(str(path.relative_to(payload_dir)) for path in offenders)
        raise PayloadBuildError(
            "the payload contains a nested installer, which must never ship "
            f"inside another artifact:\n  {listing}"
        )


def verify(payload_dir: Path) -> PayloadReport:
    """Every claim the manifest makes, re-derived from the staged bytes."""
    assert_no_nested_installers(payload_dir)
    from scripts.payload_rbox import PayloadRboxError, assert_rbox_absent

    try:
        assert_rbox_absent(payload_dir)
    except PayloadRboxError as exc:
        raise PayloadBuildError(str(exc)) from exc
    from scripts.payload_google_oauth import PayloadOAuthError, verify_bundled_oauth

    try:
        verify_bundled_oauth(payload_dir)
    except PayloadOAuthError as exc:
        raise PayloadBuildError(str(exc)) from exc
    from scripts.payload_telemetry import PayloadTelemetryError, verify_bundled_telemetry

    try:
        verify_bundled_telemetry(payload_dir)
    except PayloadTelemetryError as exc:
        raise PayloadBuildError(str(exc)) from exc
    from scripts import license_verify, third_party_licenses

    try:
        license_verify.verify_bundled_licenses(payload_dir)
    except third_party_licenses.LicenseInventoryError as exc:
        raise PayloadBuildError(str(exc)) from exc
    verify_python_startup_stderr(payload_dir)
    verify_pth_imports(payload_dir)
    report = PayloadReport()
    verify_report = verify_imports(payload_dir)
    report.routes = int(verify_report["routes"])  # type: ignore[arg-type]
    report.waveform_backend = str(verify_report["waveform"]["selected"])  # type: ignore[index]
    report.analysis_backend = str(verify_report["analysis"]["backend"])  # type: ignore[index]
    report.linked_libraries_scanned, report.link_violations = scan_linked_libraries(
        payload_dir
    )
    if report.link_violations:
        listing = "\n  ".join(v.describe() for v in report.link_violations)
        raise PayloadBuildError(
            "the payload links libraries that exist only on this machine; a "
            "Mac without Homebrew or those dev libraries cannot run it:\n  "
            f"{listing}"
        )
    sites, classifications = scan_runtime_loaded_libraries(payload_dir)
    report.runtime_load_sites = sites
    report.runtime_load_classifications = classifications
    return report


# ----- manifest ----------------------------------------------------------
@dataclass(frozen=True)
class PayloadIdentity:
    """Who the artifact says it is. Written once, read by the UI forever."""

    label: str | None  # None = the plain, unsuffixed product (OPS-08)
    product_name: str
    identifier: str
    app_version: str
    engine_version: str
    #: The hub a fresh install syncs to (#3870): a build-time value, so the
    #: tracked tree never names a tailnet host. None = no default hub.
    default_hub_url: str | None = None


@dataclass(frozen=True)
class StagedPayload:
    """What staging produced, so the manifest step takes one argument each."""

    runtime_source: Path
    python_version: str
    pruned: list[str]
    requirements: list[str]
    orphaned: list[str]
    stretch_asset: Path
    waveform_wheel_name: str
    waveform_wheel_sha256: str
    audio_engine_hello: dict[str, object]


def build_manifest(
    *,
    repo_root: Path,
    payload_dir: Path,
    identity_input: PayloadIdentity,
    staged: StagedPayload,
    report: PayloadReport,
) -> dict[str, object]:
    runtime_dir = payload_dir / "runtime"
    pylib_dir = payload_dir / "pylib"
    app_dir = payload_dir / "app"
    build_dir = app_dir / "apps/webui/frontend/build"
    identity = {
        # "" rather than null: manifest_stamp refuses to stamp a null into
        # the shell, and an empty label IS the plain product's identity.
        "lane_label": identity_input.label or "",
        "product_name": identity_input.product_name,
        "bundle_identifier": identity_input.identifier,
        "app_version": identity_input.app_version,
        "engine_version": identity_input.engine_version,
        "built_at_utc": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        **git_identity(repo_root),
    }
    return {
        "schema": MANIFEST_SCHEMA,
        "kind": MANIFEST_KIND,
        "identity": identity,
        **cloudsync_first_run.manifest_block(identity_input.default_hub_url),
        "runtime": {
            "python": staged.python_version,
            "source": str(staged.runtime_source),
            "pruned": staged.pruned,
        },
        "dependencies": staged.requirements,
        "excluded_dependencies": [
            {
                "name": name,
                "reason": EXCLUDED_DEPENDENCIES.get(
                    name, "orphaned: nothing else in the closure depends on it"
                ),
            }
            for name in staged.orphaned
        ],
        "spa": {
            "files": sum(1 for p in build_dir.rglob("*") if p.is_file()),
            "index_sha256": sha256_file(build_dir / "index.html"),
            "stretch_asset": staged.stretch_asset.name,
            "stretch_sha256": sha256_file(staged.stretch_asset),
        },
        "bytes": {
            "runtime": tree_bytes(runtime_dir),
            "pylib": tree_bytes(pylib_dir),
            "app": tree_bytes(app_dir),
            "total": tree_bytes(payload_dir),
        },
        "sha256": {
            "runtime": sha256_tree(runtime_dir),
            "pylib": sha256_tree(pylib_dir),
            "app": sha256_tree(app_dir),
        },
        "waveform_native": {
            "wheel": staged.waveform_wheel_name,
            "wheel_sha256": staged.waveform_wheel_sha256,
        },
        "audio_engine": {
            "path": AUDIO_ENGINE_RELATIVE,
            "sha256": sha256_file(payload_dir / AUDIO_ENGINE_RELATIVE),
            "engine": staged.audio_engine_hello["engine"],
            "protocol": staged.audio_engine_hello["protocol"],
            "device": staged.audio_engine_hello["device"],
        },
        "verification": {
            "routes": report.routes,
            "waveform_backend": report.waveform_backend,
            "analysis_backend": report.analysis_backend,
            "macho_files_scanned": report.linked_libraries_scanned,
            "external_link_references": [
                v.describe() for v in report.link_violations
            ],
            "runtime_loaded_libraries": report.runtime_load_classifications,
        },
    }


# ----- build -------------------------------------------------------------
def build(
    *,
    repo_root: Path,
    output_dir: Path,
    label_raw: str | None,
    runtime_source: Path,
    tauri_conf: Path,
    default_hub_url: str | None = None,
) -> dict[str, object]:
    label = validate_label(label_raw)
    # A malformed default hub is refused before any staging work, not after.
    cloudsync_first_run.manifest_block(default_hub_url)
    conf = json.loads(tauri_conf.read_text(encoding="utf-8"))
    product_name = lane_product_name(conf["productName"], label)
    identifier = lane_identifier(conf["identifier"], label)

    from scripts.desktop_icons import verify_desktop_icons_for_repo

    verify_desktop_icons_for_repo(repo_root)

    frontend_dir = repo_root / "apps/webui/frontend"
    build_dir = assert_spa_is_fresh(frontend_dir)
    sole_stretch_asset(build_dir)

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    stage_start = time.monotonic()
    pruned = stage_runtime(runtime_source, output_dir / "runtime")
    print(f"[TIMING] stage_runtime {time.monotonic() - stage_start:.2f}s", file=sys.stderr)

    python = output_dir / "runtime/bin/python3"
    # Gated HERE, after the cheap copy and before the expensive dependency
    # install: a runtime off the pinned line must never get far enough to
    # look like a build problem instead of an interpreter problem.
    python_version = _interpreter_version(python)
    assert_runtime_matches_pin(
        runtime_version=python_version,
        pin=read_python_pin(repo_root),
        runtime_source=runtime_source,
    )
    locked, orphaned = locked_requirements(repo_root)
    requirements = [entry.spec for entry in locked]
    install_start = time.monotonic()
    install_dependencies(locked, python, output_dir / "pylib")
    print(f"[TIMING] install_dependencies {time.monotonic() - install_start:.2f}s", file=sys.stderr)

    beatgrid_start = time.monotonic()
    try:
        runner_export = payload_beatgrid.runner_locked_export(repo_root)
        cached_checkpoint = payload_beatgrid.fetch_checkpoint(payload_beatgrid.CHECKPOINT_CACHE_DIR)
    except payload_beatgrid.PayloadBeatgridError as exc:
        raise PayloadBuildError(str(exc)) from exc
    install_dependencies(
        parse_locked_export(runner_export),
        python,
        output_dir / payload_beatgrid.RUNNER_SITE_RELATIVE,
    )
    payload_beatgrid.stage_checkpoint(output_dir, cached_checkpoint)
    payload_beatgrid.write_runner_launcher(output_dir)
    print(f"[TIMING] beatgrid_runner {time.monotonic() - beatgrid_start:.2f}s", file=sys.stderr)

    waveform_start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="opendj-waveform-wheel-") as wheel_tmp:
        wheel = build_waveform_native_wheel(repo_root, Path(wheel_tmp))
        waveform_wheel_name = wheel.name
        waveform_wheel_sha256 = sha256_file(wheel)
        install_waveform_native(wheel, python, output_dir / "pylib", requirements)
    print(f"[TIMING] waveform_native {time.monotonic() - waveform_start:.2f}s", file=sys.stderr)

    source_start = time.monotonic()
    stage_app_source(repo_root, output_dir / "app", build_dir)
    print(f"[TIMING] stage_app_source {time.monotonic() - source_start:.2f}s", file=sys.stderr)
    write_launcher(output_dir)
    audio_start = time.monotonic()
    audio_engine_hello = stage_audio_engine(repo_root, output_dir)
    print(f"[TIMING] audio_engine {time.monotonic() - audio_start:.2f}s", file=sys.stderr)
    from scripts.payload_google_oauth import PayloadOAuthError, bake_google_oauth

    try:
        bake_google_oauth(output_dir)
    except PayloadOAuthError as exc:
        raise PayloadBuildError(str(exc)) from exc
    from scripts.payload_telemetry import PayloadTelemetryError, bake_telemetry

    try:
        bake_telemetry(output_dir)
    except PayloadTelemetryError as exc:
        raise PayloadBuildError(str(exc)) from exc
    # OSSPUB-05: attribution for everything staged above travels with it. Runs
    # after every staging step so the inventory reads the final payload, and
    # before verify() so a missing file fails the build, not a later review.
    from scripts import third_party_licenses

    try:
        licenses_summary = third_party_licenses.write_payload_license_files(repo_root, output_dir)
    except third_party_licenses.LicenseInventoryError as exc:
        raise PayloadBuildError(str(exc)) from exc

    verify_start = time.monotonic()
    report = verify(output_dir)
    try:
        beatgrid_runner = payload_beatgrid.verify_bundled_beatgrid_runner(output_dir, repo_root)
    except payload_beatgrid.PayloadBeatgridError as exc:
        raise PayloadBuildError(str(exc)) from exc
    print(f"[TIMING] verify {time.monotonic() - verify_start:.2f}s", file=sys.stderr)
    staged_stretch = sole_stretch_asset(
        output_dir / "app/apps/webui/frontend/build"
    )

    manifest = build_manifest(
        repo_root=repo_root,
        payload_dir=output_dir,
        identity_input=PayloadIdentity(
            label=label,
            product_name=product_name,
            identifier=identifier,
            app_version=conf["version"],
            engine_version=_engine_version(repo_root),
            default_hub_url=default_hub_url,
        ),
        staged=StagedPayload(
            runtime_source=runtime_source,
            python_version=python_version,
            pruned=pruned,
            requirements=requirements,
            orphaned=orphaned,
            stretch_asset=staged_stretch,
            waveform_wheel_name=waveform_wheel_name,
            waveform_wheel_sha256=waveform_wheel_sha256,
            audio_engine_hello=audio_engine_hello,
        ),
        report=report,
    )
    manifest["beatgrid_runner"] = beatgrid_runner
    manifest["third_party_licenses"] = licenses_summary
    (output_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _engine_version(repo_root: Path) -> str:
    text = (repo_root / "apps/engine_core/config.py").read_text(encoding="utf-8")
    match = re.search(r'^ENGINE_VERSION:\s*str\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise PayloadBuildError(
            "cannot read ENGINE_VERSION out of apps/engine_core/config.py"
        )
    return match.group(1)


def default_runtime_source() -> Path:
    """The interpreter uv already resolved for this repo, at its own root.

    ``uv python find`` answers with the venv's symlinked python, so the real
    relocatable install is read back through sys.base_prefix. Guessing a
    directory name under ~/.local/share/uv would rot the moment uv changes
    its layout.
    """
    result = subprocess.run(
        ["uv", "run", "--no-sync", "python", "-c", "import sys; print(sys.base_prefix)"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PayloadBuildError(
            f"cannot resolve the repo interpreter through uv: {result.stderr}"
        )
    base = Path(result.stdout.strip())
    if not (base / "bin/python3").exists():
        raise PayloadBuildError(
            f"{base} is not a relocatable python install; the repo venv must be "
            "backed by a uv-managed CPython for the payload to embed one"
        )
    return base


# ----- CLI ---------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--label",
        default=os.environ.get("MDT_LANE_LABEL"),
        help="lane label; optional (MDT_LANE_LABEL by default). Unset builds "
        "the plain Open DJ; set only for bake-off lanes.",
    )
    parser.add_argument("--runtime-source", type=Path, default=None)
    parser.add_argument(
        "--default-hub-url",
        default=os.environ.get(DEFAULT_HUB_URL_ENV) or None,
        help="CloudSync hub a fresh install syncs to on first run "
        f"({DEFAULT_HUB_URL_ENV} by default). Unset ships no default hub (#3870).",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    try:
        manifest = build(
            repo_root=repo_root,
            output_dir=args.out.resolve(),
            label_raw=args.label,
            runtime_source=(args.runtime_source or default_runtime_source()).resolve(),
            tauri_conf=repo_root / "apps/desktop/src-tauri/tauri.conf.json",
            default_hub_url=args.default_hub_url,
        )
    except (PayloadBuildError, LaneLabelError, CloudSyncConfigError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    identity = manifest["identity"]
    byte_counts = manifest["bytes"]
    print(f"[OK] payload: {args.out}")
    print(f"[OK] lane: {identity['lane_label']}  id: {identity['bundle_identifier']}")
    dirty = " DIRTY" if identity["git_dirty"] else ""
    print(f"[OK] built from {identity['git_sha']} on {identity['git_branch']}{dirty}")
    print(f"[OK] python {manifest['runtime']['python']}, {len(manifest['dependencies'])} deps")
    print(
        "[OK] bytes: runtime "
        f"{byte_counts['runtime'] // 1_000_000}MB, pylib "
        f"{byte_counts['pylib'] // 1_000_000}MB, app "
        f"{byte_counts['app'] // 1_000_000}MB, total "
        f"{byte_counts['total'] // 1_000_000}MB"
    )
    verification = manifest["verification"]
    print(
        f"[OK] verified: {verification['routes']} routes, "
        f"{verification['macho_files_scanned']} Mach-O files with 0 external links, "
        f"{len(verification['runtime_loaded_libraries'])} runtime load sites classified"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
