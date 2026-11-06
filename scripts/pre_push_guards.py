"""Changed-path fast guards for `just pre-push`: the cheap checks that kept reddening main.

Tue 3 Nov 2026 main went red five ways a builder could have seen in seconds: the
`helper_carried_paths` drift guard (#8313 repeated an entry, #8444/#8445 a missing one), the
FILETREE.md map, the OSS tip audit (a CGNAT literal, then #8392, a home path whose folder name was learned as a
login), a frontend bundle budget grown without a charge file (#8429), and the `.svelte`-as-text
ratchet (GUARD-15). This module reads the branch diff against origin/main, runs only the guards
those paths can break, in parallel, and prints the exact fix for each red.

    python -m scripts.pre_push_guards run [--base origin/main]   # exit 1 on a blocking FAIL
    python -m scripts.pre_push_guards write-carry-paths          # regenerate the carry list (1-2 min)

Status per guard: PASS, FAIL (blocks: the same check CI runs, failing at this HEAD), WARN (could
not measure here, or a heuristic that needs the slow check), SKIP (no changed path can reach it).
WARN is never a pass; it says what was not checked and the command that checks it.

The carry guard is the one that is not the CI test itself. The real derivation
(`scripts.ci_plan_carry.helper_carried_paths`) takes 60 to 120 s on nucbox, almost all of it
finding the global helpers. The fast form checks what a diff can break: a repeated or vanished
entry, and an import a changed closure module adds to a carried file the list lacks. An import a
changed global-helper candidate adds, or a by-name load, warns with the deep command instead.

The OSS audit scans the whole published tree, not the changed files only: #8392's red was a login
LEARNED from one new path and FOUND in unchanged files, which a changed-files scan cannot see.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_plan import (
    CONFIG_PATH,
    REPO,
    Change,
    Config,
    PlanError,
    Scope,
    _owner_index,
    matches,
    read_changes,
    read_config,
)
from scripts.ci_plan_carry import _loads_code_by_name, _module_files, helper_carried_paths
from scripts.ci_plan_sources import _imported_names, is_test_module

FRONTEND = "apps/webui/frontend/"
# Same exclusion list as `just pre-push` step 8: frontend paths proven not to reach the build.
NOT_BUILD_INPUT = re.compile(
    r"^apps/webui/frontend/(tests/|\.storybook/|README\.md$|knip\.js$|playwright\.config\.ts$|\.nvmrc$|\.gitignore$)"
)
FILETREE_MAPPED = ("apps", "ops", "tools", "ci", "docs")
DEEP_CARRY = "uv run --no-sync pytest -q tests/scripts/test_ci_plan_carry_paths.py -k committed_path_list"


@dataclass(frozen=True)
class Guard:
    id: str
    fix: str


@dataclass(frozen=True)
class Outcome:
    guard: str
    status: str  # PASS | FAIL | WARN
    seconds: float
    detail: str


@dataclass(frozen=True)
class CarryReport:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


GUARDS: dict[str, Guard] = {
    g.id: g
    for g in (
        Guard(
            "carry-paths",
            "uv run --no-sync python -m scripts.pre_push_guards write-carry-paths && git add ci/test-scopes.yml",
        ),
        Guard(
            "filetree",
            "add or remove the paths the test names in FILETREE.md, then:"
            " uv run --no-sync pytest -q (private path)",
        ),
        Guard(
            "oss-audit",
            "rewrite each finding above (a placeholder home, a documented host alias), commit, then:"
            " uv run --no-sync python -m scripts.oss_tip_audit",
        ),
        Guard(
            "bundle-budget",
            "add apps/webui/frontend/budgets/<surface>/charges/<branch-or-pr>.json (budgets/README.md),"
            " or shrink the surface; reproduce: just bundle-budget",
        ),
        Guard(
            "svelte-source-text",
            "render the component (loadSvelteSsrModule) or add a reasoned entry to"
            " tests/unit/source-regex-test-allowlist.json; rerun: just pre-push-guards",
        ),
    )
}


# ----- pure: which guards a diff can break -----


def _is_structural(change: Change) -> bool:
    """Added, deleted, renamed or copied: the path set moved, not just a file's bytes."""
    return change.status[:1] in "ADRC"


def _can_move_the_carry_list(change: Change, config: Config, owner: Callable[[str], Scope | None]) -> bool:
    path = change.path
    if path == "ci/test-scopes.yml":
        return True
    if not path.endswith(".py"):
        return False
    if matches(path, config.helper_carried_paths or ()) or _is_helper_candidate(path, config):
        return True
    scope = owner(path)
    return _is_structural(change) and scope is not None and scope.name in config.helper_carried


def _is_helper_candidate(path: str, config: Config) -> bool:
    """A file that may be a global pytest helper: any conftest, or an `always` non-test module."""
    return path.endswith("conftest.py") or (matches(path, config.always) and not is_test_module(path))


def select_guards(changes: tuple[Change, ...], config: Config) -> dict[str, str]:
    """Guard id -> the first changed path that selected it, in GUARDS order."""
    owner = _owner_index(config)
    picks: dict[str, str] = {}

    def pick(guard: str, path: str) -> None:
        picks.setdefault(guard, path)

    for change in changes:
        path = change.path
        pick("oss-audit", path)
        if _can_move_the_carry_list(change, config, owner):
            pick("carry-paths", path)
        if path == "FILETREE.md" or (
            _is_structural(change) and ("/" not in path or path.split("/")[0] in FILETREE_MAPPED)
        ):
            pick("filetree", path)
        if path.startswith(FRONTEND) and not NOT_BUILD_INPUT.match(path):
            pick("bundle-budget", path)
        if path.startswith(FRONTEND + "tests/unit/") or (
            path.startswith(FRONTEND + "src/") and path.endswith(".svelte") and _is_structural(change)
        ):
            pick("svelte-source-text", path)
    return {g: picks[g] for g in GUARDS if g in picks}


# ----- the carry check -----


def carry_drift(changes: tuple[Change, ...], config: Config, root: Path) -> CarryReport:
    committed = config.helper_carried_paths or ()
    errors: list[str] = []
    warnings: list[str] = []
    repeated = sorted(p for p, n in Counter(committed).items() if n > 1)
    if repeated:
        errors.append(f"helper_carried_paths lists {repeated} more than once; keep one")
    gone = sorted(p for p in set(committed) if not p.endswith("/") and not (root / p).is_file())
    if gone:
        errors.append(f"helper_carried_paths lists files that do not exist: remove {gone}")
    owner = _owner_index(config)
    carried = set(config.helper_carried)
    for change in changes:
        path = change.path
        if not path.endswith(".py") or change.status.startswith("D") or not (root / path).is_file():
            continue
        in_closure = matches(path, committed)
        if not (in_closure or _is_helper_candidate(path, config)):
            continue
        text = (root / path).read_text(encoding="utf-8")
        missing = sorted(
            f
            for name in _imported_names(text, path)
            for f in _module_files(name, root)
            if (scope := owner(f)) is not None and scope.name in carried and not matches(f, committed)
        )
        if missing and (in_closure or path.endswith("conftest.py")):
            errors.append(f"{path} imports carried files the list lacks: add {missing}")
        elif missing:
            warnings.append(
                f"{path} may be a global helper and imports unlisted carried files {missing}; deep check: {DEEP_CARRY}"
            )
        if _loads_code_by_name(text, path):
            warnings.append(
                f"{path} loads code by name, which only the full derivation follows; deep check: {DEEP_CARRY}"
            )
    if any(c.path == "ci/test-scopes.yml" for c in changes) and not errors:
        warnings.append(
            f"ci/test-scopes.yml changed: entries are unique and exist, but only the deep check proves the set: {DEEP_CARRY}"
        )
    return CarryReport(tuple(errors), tuple(warnings))


def replace_carry_block(text: str, paths: frozenset[str]) -> str:
    """`text` with the `helper_carried_paths:` list replaced by `paths`, sorted; nothing else moves."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "helper_carried_paths:"), None)
    if start is None:
        raise PlanError("ci/test-scopes.yml has no `helper_carried_paths:` block")
    end = start + 1
    while end < len(lines) and lines[end].startswith("  - "):
        end += 1
    block = [f'  - "{p}"\n' for p in sorted(paths)]
    return "".join(lines[: start + 1] + block + lines[end:])


# ----- runners -----


def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False, env=env)
    except FileNotFoundError:
        return 127, f"executable not found: {argv[0]}"
    return result.returncode, (result.stdout + result.stderr).strip()


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _tap_failures(text: str, lines: int = 12) -> str:
    """Each TAP `not ok` line and the YAML block under it: the summary at the end names no test."""
    out = text.splitlines()
    picked = [ln for i, start in enumerate(out) if start.startswith("not ok") for ln in out[i : i + lines]]
    return "\n".join(picked) or _tail(text)


def _guard_carry(changes: tuple[Change, ...], config: Config, root: Path) -> tuple[str, str]:
    report = carry_drift(changes, config, root)
    if report.errors:
        return "FAIL", "\n".join(report.errors + report.warnings)
    if report.warnings:
        return "WARN", "\n".join(report.warnings)
    return "PASS", "no repeated, vanished or newly imported unlisted carried file"


def _guard_pytest(node: str, root: Path) -> tuple[str, str]:
    code, out = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node], root)
    return ("PASS" if code == 0 else "FAIL"), _tail(out)


def _guard_oss(root: Path) -> tuple[str, str]:
    env = {**os.environ, "ODJ_OSS_AUDIT_BACKEND": os.environ.get("ODJ_OSS_AUDIT_BACKEND", "auto")}
    code, out = _run([sys.executable, "-m", "scripts.oss_tip_audit"], root, env)
    findings = "\n".join(line for line in out.splitlines() if not line.startswith("exempt "))
    if code == 0:
        return "PASS", _tail(findings, 1)
    if code == 1:
        return "FAIL", _tail(findings, 25)
    return "WARN", f"could not measure (exit {code}): {_tail(findings, 3)}"


def _fresh_build(frontend: Path, changes: tuple[Change, ...], root: Path) -> bool:
    """A cached `pnpm build` newer than every changed frontend file on disk."""
    marker = frontend / "build" / "_app" / "immutable"
    if not marker.is_dir():
        return False
    built = marker.stat().st_mtime
    changed = [root / c.path for c in changes if c.path.startswith(FRONTEND) and (root / c.path).exists()]
    return all(p.stat().st_mtime <= built for p in changed)


def _guard_bundle(changes: tuple[Change, ...], root: Path) -> tuple[str, str]:
    frontend = root / FRONTEND
    if not _fresh_build(frontend, changes, root):
        return "WARN", (
            "no build newer than the changed frontend files, so the budget is UNMEASURED here;"
            " `just pre-push` step 8 builds and measures it, or run: just bundle-budget"
        )
    node = shutil.which("node")
    if node is None:
        return "WARN", (
            "node executable not on PATH, so the budget is UNMEASURED here;"
            " `just pre-push` step 8 builds and measures it, or run: just bundle-budget"
        )
    code, out = _run([node, "scripts/bundle-budget.mjs"], frontend)
    if out.startswith("executable not found:"):
        return "WARN", f"{out}; run: just bundle-budget"
    return ("PASS" if code == 0 else "FAIL"), _tail(out)


def _guard_svelte_text(root: Path) -> tuple[str, str]:
    frontend = root / FRONTEND
    if not (frontend / "node_modules" / "typescript").is_dir():
        return "WARN", (
            "apps/webui/frontend/node_modules is missing, so the ratchet is UNMEASURED;"
            " run: cd apps/webui/frontend && pnpm install --frozen-lockfile"
        )
    code, out = _run(
        [
            "node",
            "--experimental-strip-types",
            "--test",
            "--test-reporter=tap",
            "tests/unit/source-regex-test-ratchet.test.mjs",
        ],
        frontend,
    )
    return ("PASS" if code == 0 else "FAIL"), _tap_failures(out)


def _run_guard(guard: str, changes: tuple[Change, ...], config: Config, root: Path) -> Outcome:
    start = time.monotonic()
    if guard == "carry-paths":
        status, detail = _guard_carry(changes, config, root)
    elif guard == "filetree":
        status, detail = _guard_pytest("(private path)", root)
    elif guard == "oss-audit":
        status, detail = _guard_oss(root)
    elif guard == "bundle-budget":
        status, detail = _guard_bundle(changes, root)
    elif guard == "svelte-source-text":
        status, detail = _guard_svelte_text(root)
    else:
        raise PlanError(f"unhandled guard: {guard}")
    return Outcome(guard, status, time.monotonic() - start, detail)


def run_guards(base: str, root: Path = REPO) -> int:
    start = time.monotonic()
    changes = read_changes(base, "HEAD")
    config = read_config(root / "ci" / "test-scopes.yml")
    selected = select_guards(changes, config)
    for guard in GUARDS:
        if guard not in selected:
            print(f"[pre-push-guards] SKIP {guard}: no changed path reaches it")
    with ThreadPoolExecutor(max_workers=len(GUARDS)) as pool:
        outcomes = list(pool.map(lambda g: _run_guard(g, changes, config, root), selected))
    for outcome in outcomes:
        print(
            f"[pre-push-guards] {outcome.status} {outcome.guard} ({outcome.seconds:.1f}s, selected by {selected[outcome.guard]})"
        )
        if outcome.status != "PASS":
            print("  " + outcome.detail.replace("\n", "\n  "))
        if outcome.status == "FAIL":
            print(f"  FIX: {GUARDS[outcome.guard].fix}")
    failed = [o.guard for o in outcomes if o.status == "FAIL"]
    verdict = f"FAIL {failed}" if failed else "DONE"
    wall = time.monotonic() - start
    print(f"[pre-push-guards] {verdict}: {len(changes)} changed paths, {len(outcomes)} guards, {wall:.1f}s wall")
    return 1 if failed else 0


def write_carry_paths(root: Path = REPO) -> int:
    config = read_config(root / "ci" / "test-scopes.yml")
    derived = helper_carried_paths(config, root)
    path = root / CONFIG_PATH.relative_to(REPO)
    path.write_text(replace_carry_block(path.read_text(encoding="utf-8"), derived), encoding="utf-8")
    print(f"[pre-push-guards] wrote {len(derived)} helper_carried_paths entries to ci/test-scopes.yml")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the guards the branch diff can break")
    run.add_argument("--base", default="origin/main")
    sub.add_parser("write-carry-paths", help="regenerate helper_carried_paths in ci/test-scopes.yml")
    args = parser.parse_args(list(argv) if argv is not None else None)
    commands: dict[str, Callable[[], int]] = {
        "run": lambda: run_guards(args.base),
        "write-carry-paths": write_carry_paths,
    }
    try:
        return commands[args.command]()
    except PlanError as exc:
        print(f"[pre-push-guards] UNKNOWN: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
