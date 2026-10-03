"""Hash-pinned PEP 751 locks for the CI venvs, and the check that keeps them fresh.

CI used to fill its venvs with `uv pip install --exact --upgrade -r <file>`.
`--upgrade` revalidates every package's index page on every job, even with a
warm cache: 150 requests to pypi.org/simple per pytest shard, measured Mon 28
Sep 2026. When PyPI was slow to answer the shared agentbox IP, those requests
timed out and turned trunk red (issue #4252). A pylock.toml names each wheel by
URL and hash, so `uv pip sync` from it never reads an index page, and a warm
cache makes zero requests. `uv pip sync` is exact by definition: it removes any
package the lock does not name. ADR: docs/decisions/ADR-NEW-ci-venvs-sync-from-pylock.md.

Usage:
    python -m scripts.ci_lock compile [--upgrade]   # regenerate every lock (network)
    python -m scripts.ci_lock check [--repo-root P] # no network, no resolution

Requirements (mini-PRD)
- [if] `compile` runs [then] every lock in LOCKS is regenerated from its source
  with COMPILE_ARGS and stamped with the source fingerprint, [else stop] ✔︎ ✅
- [if] a lock's recorded fingerprint differs from its sources' requirement lines
  (added, removed or changed; comments and blank lines excluded) or compile
  settings [then] `check` exits 1 naming the lock, [else stop] ✔︎ ✅ 🎯
- [if] a direct requirement is missing from its lock, or the locked version
  falls outside its specifier, or a git requirement's commit differs [then]
  `check` exits 1 naming the requirement, [else stop] ✔︎ ✅ 🎯
- [if] a lock or source file is missing, a source line is an option this
  parser does not model, or a requirement does not parse [then] `check` exits
  2 (could not measure), never 0, [else stop] ✔︎ ✅ 🎯
- [if] a locked sdist or wheel has no sha256, or a VCS entry has no
  commit-id [then] `check` exits 1 naming it, so the lock stays hash-pinned
  even when hand-edited under an unchanged source fingerprint, [else stop] ✔︎ ✅ 🎯
- [if] a lock gains or loses a package that has no wheel (a VCS or sdist-only
  entry) relative to SOURCE_BUILDS [then] `check` exits 1 naming it: those are
  the only packages whose cold-cache install builds from source, with build
  dependencies resolved from the index, [else stop] ✔︎ ✅ 🎯

Acceptance tests (tests/scripts/test_ci_lock.py):
- [if] requirements.txt gains, loses or re-pins a line and the lock is not
  recompiled [then ⛔️] `check` reports drift.
- [if] only a comment in requirements.txt changes [then ⛔️] `check` passes.
- [if] a lock pins a version its specifier excludes [then ⛔️] `check` reports it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_TABLE = "music-dj-tools"
# Universal so a Mac runner can sync the same file; 3.11 is the CI interpreter.
COMPILE_ARGS = (
    "--universal",
    "--python-version",
    "3.11",
    "--generate-hashes",
    "--format",
    "pylock.toml",
)
COMPILE_COMMAND = "python -m scripts.ci_lock compile"
_INCLUDE_RE = re.compile(r"^(?:-r|--requirement)(?:\s+|=)(\S+)$")
_GIT_URL_RE = re.compile(r"^git\+(?P<url>[^@]+)@(?P<rev>[0-9a-f]{40})$")


@dataclass(frozen=True)
class Lock:
    source: str  # requirements file the lock is compiled from, repo-relative
    output: str  # pylock.<name>.toml, repo-relative


LOCKS = (
    Lock("requirements-ci.in", "pylock.ci.toml"),
    Lock("requirements-release-check.in", "pylock.release-check.toml"),
    Lock("requirements-docs.txt", "pylock.docs.toml"),
)
# Locked packages with no wheel. A warm cache holds their built wheel, so the
# hot path stays offline; on a cold cache (new runner, bumped pin) uv builds
# them with isolated build dependencies resolved from the index. Named here so
# adding one is a reviewed decision rather than a silent widening of that path.
SOURCE_BUILDS = frozenset({"madmom", "webrtcvad"})


class Unmeasurable(Exception):
    """The check could not measure; never read as a pass."""


# -----------------------------------------------------------------------------
# source parsing
# -----------------------------------------------------------------------------
def _strip_comment(line: str) -> str:
    # pip's rule: `#` starts a comment at line start or after whitespace.
    return re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()


def requirement_lines(source: Path) -> list[str]:
    """Every requirement line reachable from source, `-r` includes expanded, comments dropped."""
    if not source.is_file():
        raise Unmeasurable(f"missing requirements source: {source}")
    lines: list[str] = []
    for raw in source.read_text(encoding="utf-8").splitlines():
        line = " ".join(_strip_comment(raw).split())
        if not line:
            continue
        include = _INCLUDE_RE.match(line)
        if include:
            lines.extend(requirement_lines(source.parent / include.group(1)))
        elif line.startswith("-"):
            raise Unmeasurable(f"{source}: option line not modeled by scripts/ci_lock.py: {line!r}")
        else:
            lines.append(line)
    return lines


def _parse(line: str) -> Requirement:
    try:
        return Requirement(line)
    except InvalidRequirement as exc:
        raise Unmeasurable(f"unparseable requirement {line!r}: {exc}") from exc


def source_fingerprint(source: Path) -> str:
    """sha256 over the sorted requirement lines plus compile settings."""
    payload = {"args": list(COMPILE_ARGS), "requirements": sorted(requirement_lines(source))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


# -----------------------------------------------------------------------------
# lock reading and comparison
# -----------------------------------------------------------------------------
def _read_lock(path: Path) -> dict:
    if not path.is_file():
        raise Unmeasurable(f"missing lock: {path}; run `{COMPILE_COMMAND}`")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _requirement_problems(req: Requirement, packages: list[dict]) -> list[str]:
    entries = [p for p in packages if canonicalize_name(p["name"]) == canonicalize_name(req.name)]
    if not entries:
        return [f"{req}: not in the lock"]
    if req.url:
        want = _GIT_URL_RE.match(req.url)
        if want is None:
            raise Unmeasurable(f"{req}: only git URLs pinned to a full commit are modeled")
        return [
            f"{req}: lock has {entry.get('vcs')}"
            for entry in entries
            if (entry.get("vcs") or {}).get("url") != want["url"]
            or (entry.get("vcs") or {}).get("commit-id") != want["rev"]
        ]
    return [
        f"{req}: locked {entry['version']} is outside {req.specifier}"
        for entry in entries
        if not req.specifier.contains(entry["version"], prereleases=True)
    ]


def _unpinned_artifacts(packages: list[dict]) -> list[str]:
    """Every locked file without a sha256, and every VCS entry without a commit."""
    problems: list[str] = []
    for package in packages:
        name = package["name"]
        files = [("sdist", package["sdist"])] if "sdist" in package else []
        files += [("wheel", wheel) for wheel in package.get("wheels") or []]
        problems.extend(
            f"{name} {kind} {entry.get('url')} has no sha256"
            for kind, entry in files
            if not (entry.get("hashes") or {}).get("sha256")
        )
        if "vcs" in package and not package["vcs"].get("commit-id"):
            problems.append(f"{name} vcs has no commit-id")
        if not files and "vcs" not in package:
            problems.append(f"{name} has no sdist, wheel or vcs source")
    return problems


def lock_problems(lock: Lock, root: Path = REPO_ROOT) -> list[str]:
    """Every way a lock disagrees with its source; empty means fresh."""
    source = root / lock.source
    doc = _read_lock(root / lock.output)
    problems: list[str] = []
    recorded = doc.get("tool", {}).get(TOOL_TABLE, {}).get("source-sha256")
    if recorded != source_fingerprint(source):
        problems.append(f"{lock.output}: source fingerprint {recorded} is stale for {lock.source}")
    packages = doc.get("packages") or []
    problems.extend(f"{lock.output}: {p}" for p in _unpinned_artifacts(packages))
    source_only = {canonicalize_name(p["name"]) for p in packages if not p.get("wheels")}
    problems.extend(
        f"{lock.output}: {name} has no wheel and is not in SOURCE_BUILDS"
        for name in sorted(source_only - SOURCE_BUILDS)
    )
    for line in requirement_lines(source):
        problems.extend(
            f"{lock.output}: {p}" for p in _requirement_problems(_parse(line), packages)
        )
    return problems


# -----------------------------------------------------------------------------
# compile
# -----------------------------------------------------------------------------
def compile_lock(lock: Lock, *, upgrade: bool, root: Path = REPO_ROOT) -> None:
    cmd = ["uv", "pip", "compile", *COMPILE_ARGS, "--quiet", "-o", lock.output, lock.source]
    if upgrade:
        cmd.append("--upgrade")
    env = os.environ | {"UV_CUSTOM_COMPILE_COMMAND": COMPILE_COMMAND}
    subprocess.run(cmd, cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
    stamp = f'\n[tool.{TOOL_TABLE}]\nsource-sha256 = "{source_fingerprint(root / lock.source)}"\n'
    with (root / lock.output).open("a", encoding="utf-8") as handle:
        handle.write(stamp)


# -----------------------------------------------------------------------------
# cli
# -----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    compile_parser = sub.add_parser("compile", help="regenerate every CI lock (network)")
    compile_parser.add_argument(
        "--upgrade", action="store_true", help="re-pin everything to latest"
    )
    check_parser = sub.add_parser(
        "check", help="verify every CI lock matches its source (no network)"
    )
    check_parser.add_argument(
        "--repo-root", type=Path, default=REPO_ROOT, help="tree to check (default: this repo)"
    )
    args = parser.parse_args(argv)

    if args.command == "compile":
        for lock in LOCKS:
            compile_lock(lock, upgrade=args.upgrade)
            print(f"[ci-lock] wrote {lock.output} from {lock.source}")
        return 0
    if args.command == "check":
        try:
            problems = [p for lock in LOCKS for p in lock_problems(lock, args.repo_root)]
            locked = set()
            for lock in LOCKS:
                packages = _read_lock(args.repo_root / lock.output).get("packages") or []
                locked |= {canonicalize_name(p["name"]) for p in packages if not p.get("wheels")}
            problems.extend(
                f"SOURCE_BUILDS names {name}, which no lock builds from source; remove it"
                for name in sorted(SOURCE_BUILDS - locked)
            )
        except Unmeasurable as exc:
            print(f"[ERROR] UNKNOWN, could not measure: {exc}", file=sys.stderr)
            return 2
        for problem in problems:
            print(f"[ERROR] {problem}", file=sys.stderr)
        if problems:
            print(
                f"[ERROR] {len(problems)} drift finding(s); run `{COMPILE_COMMAND}`",
                file=sys.stderr,
            )
            return 1
        print(f"[OK] {len(LOCKS)} CI locks match their sources")
        return 0
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
