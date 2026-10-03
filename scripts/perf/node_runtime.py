"""The Node the PERFMODE-15 capture helpers run under: resolved, and held to the repo floor.

Codex P1 r4171125801, PR #4888: a bare `node` in an argv runs whatever PATH
holds first, or raises FileNotFoundError under launchd, whose PATH is minimal.
`resolved_node()` returns an absolute path and refuses a Node below the
frontend's `engines.node` floor, naming both, before any helper starts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from scripts.perf.capture_build_identity import _REPO

_FRONTEND_PACKAGE = _REPO / "apps" / "webui" / "frontend" / "package.json"
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def parse_node_version(text: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"not a Node version: {text!r}")
    return (int(match[1]), int(match[2]), int(match[3]))


def node_floor(package_json: Path = _FRONTEND_PACKAGE) -> tuple[int, int, int]:
    """The `>=X.Y.Z` floor in the frontend's `engines.node`."""
    spec = json.loads(package_json.read_text(encoding="utf-8"))["engines"]["node"]
    if not spec.startswith(">="):
        raise ValueError(f"{package_json} engines.node is {spec!r}, not a >= floor")
    return parse_node_version(spec[2:])


def node_version_refusal(version: tuple[int, int, int], floor: tuple[int, int, int]) -> str | None:
    if version < floor:
        return f"Node {'.'.join(map(str, version))} is below the frontend floor {'.'.join(map(str, floor))}"
    return None


def resolved_node(search_path: str | None = None) -> str:
    """Absolute path of the `node` on `search_path` (default: this process's PATH), at or above the floor."""
    search = os.environ.get("PATH", "") if search_path is None else search_path
    node = shutil.which("node", path=search)
    if node is None:
        raise SystemExit(f"node is not on PATH ({search!r}): the PERFMODE-15 capture helpers run under Node")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30, check=True).stdout
    refusal = node_version_refusal(parse_node_version(version), node_floor())
    if refusal is not None:
        raise SystemExit(f"{node}: {refusal}")
    return node
