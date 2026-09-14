"""Shared preflight: the local `gh` CLI must be new enough for `--slurp`.

`gh api --paginate --slurp` is required by `scripts/review_gh.py` (imported by
`scripts/ci_wait.py`) and by `scripts/periodic_window.py` to merge paginated
GitHub API responses into one JSON array. `--slurp` landed in gh 2.64.0
(cli/cli release notes, Dec 2024); an older `gh` dies with the opaque
`unknown flag: --slurp`, naming neither the missing flag's origin nor how to
fix it.

Confirmed live (Fri 12 Sep 2026 onward): agentbox-15 (job 103871571683) ran
`gh` 2.62.0 at `/root/.local/bin/gh` and every scheduled `periodic-checks.yml`
run since has died in its first job on exactly this error, skipping all 13
downstream jobs. Fixed at the fleet level by upgrading the runner's `gh`; this
module is the fail-fast half so the NEXT stale-`gh` runner (this fleet has
drifted before -- `nucbox-wsl` carries the same `agentbox` label) reports a
named cause instead of reproducing the same silent multi-day outage.

Split into pure logic (`parse_gh_version`, `check_min_version`, both testable
on literal strings/tuples) and a thin live wrapper (`_live_gh_version`,
`require_gh_min_version`) so tests never mock `gh` itself (AGENTS.md: no
mocked APIs or outputs) -- only the pure comparison is unit-tested directly,
and the live path is proven by one real `gh --version` call, mirroring
`scripts.review_gh._flatten_pages` / `_paginated_json_list`.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache

#: --slurp's minimum. See module docstring for the release citation.
MIN_GH_VERSION: tuple[int, int, int] = (2, 64, 0)

_VERSION_LINE = re.compile(r"gh version (\d+)\.(\d+)\.(\d+)")


class GhVersionError(RuntimeError):
    """The installed `gh` CLI is older than this tooling requires."""


def _fmt(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def parse_gh_version(raw: str) -> tuple[int, int, int]:
    """Parse `gh --version`'s first line into a (major, minor, patch) tuple.

    Pure: fed the CLI's literal text, never a stand-in for a live call.
    """
    match = _VERSION_LINE.search(raw)
    if not match:
        raise GhVersionError(f"could not parse a version from `gh --version` output: {raw!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def check_min_version(
    installed: tuple[int, int, int], minimum: tuple[int, int, int] = MIN_GH_VERSION
) -> None:
    """Raise GhVersionError, naming both versions, when `installed` < `minimum`.

    Pure comparison, kept separate from the live `gh --version` shellout so
    it is directly testable on literal tuples.
    """
    if installed < minimum:
        raise GhVersionError(
            f"gh {_fmt(installed)} is older than the minimum {_fmt(minimum)} this "
            f"tooling requires (needs --slurp, added in gh 2.64.0). "
            f"Upgrade gh on this runner rather than removing --slurp."
        )


@lru_cache(maxsize=1)
def _live_gh_version() -> tuple[int, int, int]:
    """The real, installed `gh --version`. Cached: one subprocess per process,
    not one per `gh api` call -- callers may invoke `_gh()` / `Gh._load()`
    many times in a single script run."""
    proc = subprocess.run(["gh", "--version"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GhVersionError(
            f"`gh --version` failed ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip() or '<no output>'}"
        )
    return parse_gh_version(proc.stdout)


def require_gh_min_version(
    installed: tuple[int, int, int] | None = None,
    minimum: tuple[int, int, int] = MIN_GH_VERSION,
) -> None:
    """Fail fast, naming both versions, if the local `gh` is too old.

    `installed` defaults to the real live `gh --version` (cached); tests pass
    an explicit tuple instead of mocking the subprocess call.
    """
    check_min_version(installed if installed is not None else _live_gh_version(), minimum)
