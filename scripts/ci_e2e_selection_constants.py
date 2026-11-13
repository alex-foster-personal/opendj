"""Constants, patterns and shared types for the root Playwright e2e spec selector."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

from scripts.affected_tests import REPO, build_graph, reverse_reach
from scripts.ci_commented_list import read_commented_list
from scripts.ci_required_gate import merge_commit_changed_files
from scripts.e2e_footprints_refresh import GATES

SWITCH_VARIABLE = "CI_E2E_SPEC_SELECTION"
SWITCH_ON = "affected"
SCHEMA = 2
RECORD_NAME = "e2e-selection.json"
SPECS_NAME = "e2e-specs.txt"
FRONTEND = "apps/webui/frontend/"
SPECS_DIR = "tests/e2e/"
SEED_MAP = FRONTEND + SPECS_DIR + ".spec_footprints.json"
ALWAYS_RUN_NAME = "ci/e2e-always-run.txt"
BUDGET_NAME = "ci/e2e-budget.json"
MAX_MAP_AGE_DAYS = 7
#: Python packages the root suite boots: a changed module any of them imports is FULL.
ENGINE_PREFIXES = ("apps.engine_core", "apps.webui.server")
#: Named gates no request log can footprint: production builds, a no-server spec, a shell
#: script, a Python helper that owns its own fixture. They run on any frontend change and
#: are priced by static_gates_min so an unmeasured gate is never free to the budget.
STATIC_GATES = ("webkit", "deckload-smoke", "stretch", "master12", "onboarding", "cmdk-pick-race", "cmdk-preview-cue")
#: Which specs each static gate runs (their own configs, root testIgnore in
#: playwright.config.ts): a changed one of these promotes its gate to strong, since the root
#: filter would match nothing for it. Prefixes, relative to the frontend.
STATIC_GATE_SPECS: dict[str, tuple[str, ...]] = {
    "webkit": (
        "tests/e2e/webkit-deckload.spec.ts",
        "tests/e2e/stem-dial-drag-webkit.spec.ts",
        "tests/e2e/playlist-detail.spec.ts",
        "tests/e2e/setup-entry-points.spec.ts",
    ),
    "deckload-smoke": ("tests/e2e/deckload-smoke.spec.ts",),
    "stretch": ("tests/e2e/stretch-quality.spec.ts",),
    "master12": ("tests/e2e/audio-output-topology.spec.ts",),
    "onboarding": ("tests/e2e/onboarding-gauntlet-",),
    "cmdk-pick-race": ("tests/e2e/cmdk-pick-race.spec.ts",),
    "cmdk-preview-cue": ("tests/e2e/cmdk-preview-cue.spec.ts",),
}
#: Dev-server gates e2e.yml runs after it builds the map (S1 round 34, so a map that times
#: out on main is still built), so no map holds their specs: a changed one of these promotes
#: its gate to strong without the map (Codex on PR #10636). Prefixes, relative to the
#: frontend; their configs' testMatch, pinned by (private path).
GATES_AFTER_THE_MAP: dict[str, tuple[str, ...]] = {
    "library-jobs": ("tests/e2e/library-jobs-ordering.spec.ts",),
    "preflight": ("tests/e2e/preflight-gate.spec.ts", "tests/e2e/fresh-install-onboarding.spec.ts"),
}
ALL_GATES = tuple(sorted(GATES)) + STATIC_GATES
HEX40 = re.compile(r"^[0-9a-f]{40}$")
REASON_ALWAYS = "always run (ci/e2e-always-run.txt)"
REASON_CHANGED_SPEC = "changed spec"
STRONG_PREFIXES = (REASON_ALWAYS, REASON_CHANGED_SPEC)

#: A change to any of these runs the full root suite. `*` crosses `/` (fnmatch).
FULL_PATTERNS: tuple[tuple[str, str], ...] = (
    (FRONTEND + "tests/e2e/support/*", "the shared e2e support code and fixtures builders"),
    (FRONTEND + "tests/e2e/fixtures/*", "the shared e2e fixtures"),
    (FRONTEND + "tests/e2e/*.config.ts", "a Playwright or Vite config of the e2e tree"),
    (FRONTEND + "tests/e2e/*-endpoints.ts", "an engine endpoint helper the e2e configs share"),
    (FRONTEND + "playwright*.config.ts", "the root Playwright config"),
    (FRONTEND + "vite*.ts", "the Vite config or a Vite plugin every page is served through"),
    (FRONTEND + "svelte.config.js", "the SvelteKit config"),
    (FRONTEND + "tsconfig*.json", "the TypeScript config the dev server compiles with"),
    (FRONTEND + "webui-port-config.ts", "the port claim every suite boots through"),
    (FRONTEND + "package.json", "the frontend's package manifest"),
    (FRONTEND + "pnpm-lock.yaml", "the frontend's dependency lock"),
    (FRONTEND + "static/*", "a static asset served outside the module graph"),
    (FRONTEND + "src/app.html", "the document every route is served in"),
    (FRONTEND + "src/service-worker*", "the service worker"),
    # Server-executed SvelteKit modules run inside the dev server, so no browser request
    # ever records them in a footprint: a change there is FULL, not unrelated (Codex,
    # PR #7948: `hooks.server.ts` injects the full-reload receiver a root spec tests).
    (FRONTEND + "src/hooks.server.*", "a server hook every dev-server response runs through"),
    (FRONTEND + "src/*.server.ts", "a server-only module no browser request records"),
    (FRONTEND + "src/*/+server.ts", "a server endpoint no browser request records"),
    (SEED_MAP, "the footprint map itself"),
    (ALWAYS_RUN_NAME, "the always-run list"),
    (BUDGET_NAME, "the e2e budget"),
    ("scripts/ci_commented_list.py", "the always-run list parser this selector reads"),
    ("scripts/ci_e2e_selection.py", "this selector"),
    ("scripts/e2e_footprints_refresh.py", "the map builder"),
    ("scripts/affected_tests.py", "the import graph this selector reads the engine's reach from"),
    ("scripts/ci_required_gate.py", "the scope decision this selector runs inside"),
)

#: A change here is an ENGINE change: every spec and gate is a candidate with no footprint
#: to tell them apart, and the budget takes the cheapest (the maintainer's hard cap, Fri 30 Oct 2026;
#: before it these were FULL, which the replay measured as 62 of 65 FULL merges in 150).
ENGINE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("apps/webui/server/*", "the engine every spec boots"),
    ("apps/webui/*.py", "the webui package root (the port claim, the server entry)"),
    # The gate installs the engine's dependencies with `uv sync --locked` from these two, so
    # a dependency update changes the engine every API-backed spec talks to (Codex, PR #7948).
    ("pyproject.toml", "the Python dependency manifest the gate installs the engine from"),
    ("uv.lock", "the Python dependency lock the gate installs the engine from"),
)
#: A file under the e2e tree that is not a spec (an allow list a spec reads, a timeouts
#: module a config imports) has no footprint and no import edge the selector can see, so a
#: change to one is FULL rather than a note: the audit a changed allow list feeds must run.
REASON_E2E_INPUT = "an input of the e2e tree outside a spec (no footprint names its reader)"
REASON_ENGINE = "engine change"

#: One shell seam: `git -C <repo> <args...>` -> stdout. Tests replace it.
Git = Callable[[Sequence[str]], str]


class FullSuite(Exception):
    """The change set cannot be narrowed; the message says why."""
