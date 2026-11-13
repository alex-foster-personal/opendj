"""Shared constants, dataclasses and small helpers for crate sync.

Split out of :mod:`apps.webui.crate_sync` (issue #9687) so the sibling
modules share one vocabulary without importing the CLI facade;
``crate_sync`` re-exports every name here.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.shared import platform_paths
from apps.shared.platform_paths import PROJECT_ROOT

# Re-export, not a copy: the one prefix set lives in platform_paths (T3b D1).
STREAMING_PREFIXES: tuple[str, ...] = platform_paths.STREAMING_PREFIXES
PRELOAD1_PRESET: Path = (
    PROJECT_ROOT / "apps" / "webui" / "frontend" / "src" / "routes" / "performance" / "preload1" / "preset.ts"
)
DEFAULT_CRATE_ROOT: Path = Path("/data/mdt-crate")
REMOTE_REPO: Path = Path("/root/music-dj-tools")
#: Env naming this deployment's allowed owner-Mac SSH target(s) (user@host or
#: user@tailnet-name, os.pathsep-separated; first entry is the --owner
#: default). No personal tailnet identity is hardcoded here (#910); unset
#: fails fast, only at the point a --remote pull actually needs it, because
#: validating against an empty allowlist would silently refuse everyone
#: while looking like a real check.
OWNER_SSH_TARGETS_ENV: str = "MDT_OWNER_SSH_TARGETS"
#: Env naming this deployment's owner-Mac repo checkout path, used to sanity
#: check the path a --remote pull is told to trust (#910). Unset fails fast
#: the same way, and only when --remote pull needs it.
OWNER_REPO_ENV: str = "MDT_OWNER_REPO"
#: Env naming the private key this deployment's owner-Mac pull lane
#: authenticates with. A path under one machine's /root is deployment config,
#: not repo content, so it sits beside OWNER_SSH_TARGETS_ENV rather than as a
#: literal (#1540). Unset fails fast at the point a pull needs it, the same
#: contract as the two env names above.
OWNER_SSH_KEY_ENV: str = "MDT_OWNER_SSH_KEY"
ANLZ_SIBLING_SUFFIXES: frozenset[str] = frozenset({".DAT", ".EXT", ".2EX"})
ARTWORK_SIBLINGS: tuple[str, ...] = ("artwork.jpg", "artwork_s.jpg", "artwork_m.jpg")
FileKind = Literal["audio", "anlz", "artwork"]
DestKind = Literal["ssh", "local"]


@dataclass(frozen=True)
class CrateFile:
    source: Path
    dest: Path
    kind: FileKind
    size_bytes: int
    mtime_s: int = 0
    stable_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SyncPlan:
    scope: str
    files: tuple[CrateFile, ...]
    skipped_streaming: int
    skipped_absent: int


# ----- preload1 ids -------------------------------------------------------


def preload1_stable_ids(preset_path: Path = PRELOAD1_PRESET) -> tuple[str, ...]:
    """Read the four preload1 deck ids from the TypeScript preset. No copy."""
    if not preset_path.is_file():
        raise RuntimeError(f"preload1 preset missing: {preset_path}")
    ids = tuple(re.findall(r"stable_id:\s*'([0-9a-f]{40})'", preset_path.read_text(encoding="utf-8")))
    if len(ids) != 4:
        raise RuntimeError(f"preload1 preset {preset_path} must declare 4 stable_id values, got {len(ids)}")
    return ids


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def plan_bytes(plan: SyncPlan) -> int:
    return sum(item.size_bytes for item in plan.files)
