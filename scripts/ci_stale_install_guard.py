"""Fail when THIS interpreter can import music-dj-tools from outside the checkout.

Run it WITH the interpreter under test (``<python> scripts/ci_stale_install_guard.py
--workspace <checkout>``); it inspects that interpreter's own import machinery.
Stdlib only, so it runs on any CI interpreter (toolcache 3.11, system 3.12) before
any project dependency is installed. Called by scripts/ci_runner_preflight.sh.

Supersedes: nothing; no earlier check inspected a job's interpreter for a project
install. It EXTENDS scripts/ci_runner_preflight.sh rather than replacing it: the
preflight still names missing executables, then runs this guard for each of
``python``/``python3`` on PATH. Every self-hosted job that launches Python calls
the preflight first, pinned by tests/scripts/test_ci_workflow_stale_install_coverage.py.

WHY (Mon 28 Sep 2026). The setup-python toolcache interpreter on agentbox held a
job-installed copy of this project: music_dj_tools-1.0.1.dist-info plus 469
``apps/`` files and ``_rb_waveform_native.abi3.so``, written Fri 4 Sep 2026 22:44
UTC by run 33822960707 ("pytest fast lane + reqs-check", a stale PR branch). That
run's ci.yml passed ``PY="$(command -v python)"`` (the toolcache interpreter) to
``make waveform-native-release-check``, whose ``uv pip install --python $(PY)
--reinstall`` then wrote the wheel into the shared interpreter every later job on
that host inherits (and agbox2/3 inherited it again by rsync). A stale project
install in a shared interpreter shadows the checkout: tests and tools silently
import old code. The rule (ADR PR #4214): fail any job whose interpreter can import
the project from outside ``$GITHUB_WORKSPACE``.

WHAT COUNTS
  - any installed distribution whose normalized name is ``music-dj-tools``
    (PEP 503: ``Music_DJ.Tools`` matches), located outside the workspace;
  - ``importlib.util.find_spec`` of each top-level import name the project ships
    (``TOP_LEVEL_IMPORTS``, pinned to pyproject.toml by a test) resolving outside
    the workspace.
  A distribution outside the workspace is still ALLOWED when it is an editable
  install whose ``direct_url.json`` points INTO the workspace, because it then
  imports the checkout's own code.

Exit codes: 0 clean, 1 at least one shadowing install found, 2 usage error.

Requirements (``[if] scenario [then broken]``):
  ✔︎ ✅ 🎯 R1 flags a project distribution outside the workspace
    - if a planted music_dj_tools-0.0.0.dist-info in site-packages exits 0 then broken
    - if the failure message does not name the dist-info path then broken
    - if ``Name: Music_DJ.Tools`` (un-normalized) is not flagged then broken
  ✔︎ ✅ 🎯 R2 flags a top-level import name resolving outside the workspace
    - if a bare ``apps/`` package in site-packages exits 0 then broken
    - if a bare ``_rb_waveform_native`` module in site-packages exits 0 then broken
    - if TOP_LEVEL_IMPORTS drifts from pyproject.toml's packages/ext-modules then broken
  ✔︎ ✅ 🎯 R3 allows what imports the checkout itself
    - if a clean interpreter exits nonzero then broken
    - if a venv INSIDE the workspace holding the dist-info exits nonzero then broken
    - if an editable install pointing into the workspace exits nonzero then broken
    - if an editable install pointing OUTSIDE the workspace exits 0 then broken
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

PROJECT_DIST_NAME = "music-dj-tools"
# Pinned to pyproject.toml ([tool.setuptools.packages.find] include roots plus
# [[tool.setuptools-rust.ext-modules]] targets) by
# tests/scripts/test_ci_stale_install_guard.py.
TOP_LEVEL_IMPORTS = ("apps", "_rb_waveform_native")
TAG = "[stale-install-guard]"


@dataclass(frozen=True)
class Shadow:
    what: str
    path: str


# ---------------------------------------------------------------------------
# helpers


def _normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _is_inside(path: str, root: str) -> bool:
    real = os.path.realpath(path)
    return os.path.commonpath([real, root]) == root


def _editable_target(dist: importlib.metadata.Distribution) -> str | None:
    raw = dist.read_text("direct_url.json")
    if raw is None:
        return None
    direct_url = json.loads(raw)
    if not direct_url.get("dir_info", {}).get("editable", False):
        return None
    return unquote(urlparse(direct_url["url"]).path)


def _dist_location(dist: importlib.metadata.Distribution) -> str:
    # PathDistribution._path is the .dist-info/.egg-info dir itself; every
    # distribution importlib.metadata discovers on sys.path is a PathDistribution.
    return str(dist._path)  # type: ignore[attr-defined]


def _spec_locations(name: str) -> list[str]:
    spec = importlib.util.find_spec(name)
    if spec is None:
        return []
    locations = list(spec.submodule_search_locations or [])
    if spec.origin and os.path.isabs(spec.origin):
        locations.append(spec.origin)
    return locations


# ---------------------------------------------------------------------------
# checks


def find_shadowing_distributions(workspace: str) -> list[Shadow]:
    shadows = []
    for dist in importlib.metadata.distributions():
        if _normalize_dist_name(dist.metadata["Name"] or "") != PROJECT_DIST_NAME:
            continue
        location = _dist_location(dist)
        if _is_inside(location, workspace):
            continue
        editable_target = _editable_target(dist)
        if editable_target is not None and _is_inside(editable_target, workspace):
            continue
        shadows.append(Shadow(f"distribution {dist.metadata['Name']} {dist.version}", location))
    return shadows


def find_shadowing_imports(workspace: str) -> list[Shadow]:
    return [
        Shadow(f"import name {name!r}", location)
        for name in TOP_LEVEL_IMPORTS
        for location in _spec_locations(name)
        if not _is_inside(location, workspace)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workspace", required=True, help="the job's checkout root ($GITHUB_WORKSPACE)"
    )
    args = parser.parse_args(argv)
    workspace = os.path.realpath(args.workspace)
    if not os.path.isdir(workspace):
        print(f"{TAG} ERROR: workspace {workspace!r} is not a directory", file=sys.stderr)
        return 2

    shadows = find_shadowing_distributions(workspace) + find_shadowing_imports(workspace)
    if shadows:
        print(
            f"::error::{TAG} interpreter {sys.executable} can import {PROJECT_DIST_NAME} "
            f"from outside the checkout {workspace}; a stale install there shadows the "
            "code under test",
            file=sys.stderr,
        )
        for shadow in shadows:
            print(f"{TAG}   {shadow.what}: {shadow.path}", file=sys.stderr)
        print(
            f"{TAG} fix the runner, not the job: remove the dist-info above and exactly "
            "the files its RECORD lists from this interpreter's site-packages (back them "
            "up first). Install the project only into a job venv (uv venv / "
            "scripts/ci_venv.sh), never into the shared interpreter.",
            file=sys.stderr,
        )
        return 1
    print(f"{TAG} ok: {sys.executable} has no {PROJECT_DIST_NAME} install outside {workspace}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
