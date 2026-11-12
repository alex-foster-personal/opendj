"""perf-lab: the environment every process the harness launches gets, by allowlist.

A process inherits the operator's whole environment unless told otherwise, and
a denylist can never be complete: UV_PYTHON or UV_INDEX_URL change which
interpreter or packages an install lays down, npm_config_* and PNPM_* change
pnpm's artifacts, NODE_OPTIONS and PYTHON* change a runtime, GIT_DIR or
GIT_INDEX_FILE change which tree git reads, and MUSIC_DJ_* port variables
change which ports a claim returns, all without reaching any record of the
run. So every launch takes a named builder below, each the allowlisted part of
the operator's environment for one purpose, compared upper-cased as Windows
compares names:

  base_env     programs, home, temp space, the Windows process basics
  install_env  base_env + locale, proxy, CA and cache-location variables the
               locked installers need to reach the network (stack.py installs)
  git_env      base_env (stack.py's git reads: no GIT_* redirection)
  tool_env     base_env + explicit extras (stack.py's port claim)
  harness_env  base_env + display/session locators and
               PLAYWRIGHT_BROWSERS_PATH (ab.py's abwin.mjs under xvfb-run)
  stack_env    in stack.py: base_env + the stack's own settings (engine, vite)
  browserEnv   in browser-env.mjs: the same rule for every measured Chromium

Launches documented as env-insensitive instead: ab.py runs stack.py with the
operator's environment on purpose (stack.py reads PERFLAB_SCRATCH and filters
every launch of its own through these builders); node_runtime.py's
`node --version` prints a version no variable changes; the sampler, analyzer,
heap diff and ledger launch nothing. driver.mjs and the sampler are started by
the operator (SKILL.md), so their own environment is the operator's; what they
measure is the browser (browserEnv) and the stack (stack_env).

Not neutralized: the installers' user configuration files (~/.npmrc,
~/.config/uv/uv.toml), which hold registry and proxy settings a network needs.
"""

from __future__ import annotations

from collections.abc import Mapping

BASE_INHERITED = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TEMP",
        "TMP",
        # Windows: a process cannot start, or find its profile, without these
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
    }
)

# where to reach the network and how to trust it, where caches live, and how
# to decode text: never which interpreter, index, registry or artifacts
INSTALL_INHERITED = BASE_INHERITED | {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
}

# Named here so the rule is checkable: no builder may pass these, whatever an
# allowlist above grows to hold.
INSTALL_DROPPED_PREFIXES = ("UV_", "PIP_", "NPM_CONFIG_", "PNPM_")
INSTALL_DROPPED = frozenset({"NODE_OPTIONS", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"})

HARNESS_INHERITED = BASE_INHERITED | {
    "DISPLAY",
    "XAUTHORITY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "PLAYWRIGHT_BROWSERS_PATH",
}


def _allow(inherited: Mapping[str, str], names: frozenset[str]) -> dict[str, str]:
    env = {k: v for k, v in inherited.items() if k.upper() in names}
    bad = [k for k in env if k.upper() in INSTALL_DROPPED or k.upper().startswith(INSTALL_DROPPED_PREFIXES)]
    if bad:  # an allowlist edit that let one through is a bug, never a setting
        raise ValueError(f"environment allowlist passes dropped variables: {sorted(bad)}")
    return env


def base_env(inherited: Mapping[str, str]) -> dict[str, str]:
    """Programs, home, temp space and the Windows process basics."""
    return _allow(inherited, BASE_INHERITED)


def install_env(inherited: Mapping[str, str]) -> dict[str, str]:
    """The locked installers' environment (uv sync --locked, pnpm install
    --frozen-lockfile): base_env plus locale, proxy, CA and cache locations.
    UV_*, PIP_*, npm_config_*, PNPM_*, NODE_OPTIONS and VIRTUAL_ENV never pass,
    so the lockfile alone decides what is installed and uv syncs the
    worktree's own .venv."""
    return _allow(inherited, frozenset(INSTALL_INHERITED))


def git_env(inherited: Mapping[str, str]) -> dict[str, str]:
    """git reads of a worktree: no GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE or
    GIT_CONFIG_* can point them at another tree or change what they report."""
    return base_env(inherited)


def tool_env(inherited: Mapping[str, str], **extra: str) -> dict[str, str]:
    """base_env plus explicit settings, for a one-shot tool such as the port claim."""
    return {**base_env(inherited), **extra}


def harness_env(inherited: Mapping[str, str]) -> dict[str, str]:
    """A harness runner (abwin.mjs under xvfb-run): base_env plus where the
    display, session and Playwright's browsers are, never how they behave."""
    return _allow(inherited, frozenset(HARNESS_INHERITED))


# What the harness RECORDS rather than filters: the variables that change how a
# measuring process itself runs (its allocator, loader, interpreter or runtime
# flags, thread pools, locale and time zone, Playwright's own switches).
# driver.mjs and the sampler are started by the operator, so instead of
# re-executing under an allowlist each records this part of the environment it
# really ran under (manifest.measurement_deps, harness.mjs jsMeasurementDeps),
# and ledger.capture_harness folds it into the capture's harness identity: two
# captures whose instruments ran under another NODE_OPTIONS never compare.
# Every name INSTALL_DROPPED removes for being a runtime setting is recorded;
# a locator any builder passes (PATH, HOME, TMPDIR, DISPLAY ...) never is,
# since it moves between shells and sessions without changing a measurement,
# and recording it would make no two captures comparable. harness.mjs holds
# the same two lists for the Node instruments (a test compares them).
INSTRUMENT_RECORDED = (INSTALL_DROPPED - {"VIRTUAL_ENV"}) | {
    "UV_THREADPOOL_SIZE",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_BIND_NOW",
    "GLIBC_TUNABLES",
    "TZ",
    "LANG",
    "DEBUG",
}
INSTRUMENT_RECORDED_PREFIXES = (
    "NODE_",
    "PYTHON",
    "MALLOC",
    "DYLD_",
    "LC_",
    "JEMALLOC",
    "TCMALLOC",
    "OMP_",
    "OPENBLAS_",
    "MKL_",
    "PLAYWRIGHT_",
    "PW_",
)
# locators a recorded prefix would match: where CA certificates and
# Playwright's browsers are, never how anything runs (the browser itself is
# hashed into the stack manifest)
INSTRUMENT_NOT_RECORDED = frozenset({"PLAYWRIGHT_BROWSERS_PATH", "NODE_EXTRA_CA_CERTS"})
# a builder's allowlisted name that is a setting, not a locator: the locale
# changes how text is decoded and formatted, so it is passed and recorded
_LOCALE = frozenset({"LANG", "LC_ALL", "LC_CTYPE"})


def _recorded(name: str) -> bool:
    up = name.upper()
    if up in INSTRUMENT_NOT_RECORDED:
        return False
    return up in INSTRUMENT_RECORDED or up.startswith(INSTRUMENT_RECORDED_PREFIXES)


def instrument_env(inherited: Mapping[str, str]) -> dict[str, str]:
    """The part of a measuring process's own environment that changes how it
    runs, sorted by name, which it records in its measurement_deps. Raises if
    the rule would record a locator some builder passes (anything allowlisted
    but the locale): that would make every capture incomparable."""
    leaked = sorted(n for n in (HARNESS_INHERITED | INSTALL_INHERITED) - _LOCALE if _recorded(n))
    if leaked:  # a rule edit that records a locator is a bug, never a setting
        raise ValueError(f"instrument_env would record locators: {leaked}")
    return {k: v for k, v in sorted(inherited.items()) if _recorded(k)}
