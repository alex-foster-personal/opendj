"""Which security areas `scan (pr)` runs at a pull request head (CI fan-out, Thu 5 Nov 2026).

Secrets run on every head: gitleaks over merge-base..head takes seconds. The full set
(deps, SAST, workflow audit) runs only when the PR changes a supply-chain input below.
The decision is made inside the job, not by a workflow `paths` filter, so the `scan (pr)`
check exists on every head and ci-wait keeps expecting it.

Fails closed: if the changed-files list cannot be read, every area runs.

Usage: python3 -m scripts.security.pr_scan_scope --base <sha> --head <sha>
Prints `areas=<secrets|all>` and appends it to $GITHUB_OUTPUT when that is set.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from collections.abc import Sequence

#: A root file is listed beside its `**/` form: fnmatch's `**/x` needs a slash, GitHub's does not.
SUPPLY_CHAIN_PATHS: tuple[str, ...] = (
    ".github/**",
    "pyproject.toml",
    "**/pyproject.toml",
    "uv.lock",
    "**/uv.lock",
    "requirements*.txt",
    "**/requirements*.txt",
    "pylock*.toml",
    "**/pylock*.toml",
    "package.json",
    "**/package.json",
    "package-lock.json",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/Cargo.toml",
    "**/Cargo.lock",
    "osv-scanner.toml",
    ".gitleaks.toml",
    "tools/semgrep/**",
    "scripts/security/**",
    "tests/fixtures/security/**",
)

SECRETS_ONLY = "secrets"
ALL_AREAS = "all"


def supply_chain_files(changed_files: Sequence[str]) -> list[str]:
    return [path for path in changed_files if any(fnmatch.fnmatch(path, pattern) for pattern in SUPPLY_CHAIN_PATHS)]


def areas_for(changed_files: Sequence[str]) -> str:
    return ALL_AREAS if supply_chain_files(changed_files) else SECRETS_ONLY


def _changed_files(base: str, head: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", base, head],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    try:
        changed = _changed_files(args.base, args.head)
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"[WARN] changed files unreadable ({detail}); running every area (fail closed)")
        areas = ALL_AREAS
    else:
        matched = supply_chain_files(changed)
        areas = ALL_AREAS if matched else SECRETS_ONLY
        reason = (
            f"supply-chain inputs changed: {', '.join(matched[:10])}" if matched else "no supply-chain input changed"
        )
        print(f"[security] {len(changed)} changed file(s); {reason}")
    print(f"areas={areas}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"areas={areas}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
