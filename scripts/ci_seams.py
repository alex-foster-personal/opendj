"""Seam classification of a change set, and the seam plan the `scope` job logs (SMARTEST-CI
round 13, docs/decisions/ADR-NEW-seam-ci.md).

Every changed path maps to exactly one `Kind`. The code seams are webui (TypeScript/Svelte),
engine (Rust), desktop (Tauri) and python; the other kinds are prose (exactly what
`scripts/ci_pr_scope.py` scopes out of ci.yml), the requirements ledger, the HTTP contract
(`openapi.json`, which both webui and python consume), infra (CI-wide configuration) and
unclassified.

`plan_seams` turns a change set into the seams it touches. A plan is `full` (every seam) for
any case that must never narrow: a non-pull_request event, a Trunk queue draft
(`trunk-merge/*`), an empty diff, an infra path or an unclassified path. Narrowing can
therefore only ever apply to a PR's own head run; the queue runs everything before main.

Round 13 is SHADOW: `shadow` prints the plan and writes it to the step summary, and nothing
is skipped. Round 13 also measured why a seam-level plan alone cannot skip a job group:
webui unit tests read Python sources and `apps/desktop/` files, and Python tests read the
frontend, so both groups cross every seam. The saving needs module-level pytest selection
(round 14) and a reverse-edge reader for the webui group.

`baseline FILE` re-measures the crossing rate over a JSON list of `{number, files}` and
says UNKNOWN (exit 2) when any path is unclassified, rather than quoting a rate over a
corpus it cannot fully read.

Requirements:
- ✔︎ ✅ 🎯 Every tracked file has a class; an unknown root file is unclassified.
- ✔︎ ✅ 🎯 Prose is exactly what ci.yml's in-run scope excludes.
- ✔︎ ✅ 🎯 Unnarrowable cases select every seam.
- ✔︎ ✅ 🎯 Standard library only: the light pool's runner images carry a bare `python3`.

Acceptance tests (tests/scripts/test_ci_seams.py):
- [if] a new top-level file lands without a class [then] a test names it [⛔️ if it would
  silently select the full pipeline forever].
- [if] a webui-only diff is planned [then] it touches only the webui seam [⛔️ if the
  crossing rate counts it as a crossing].
- [if] a Trunk queue draft is planned [then] every seam is selected [⛔️ if a narrowed
  batch could reach main].
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from scripts.ci_pr_scope import IN_RUN_PULL_REQUEST_SCOPES, workflow_would_run_for_files
from scripts.ci_required_gate import merge_commit_changed_files

QUEUE_DRAFT_PREFIX = "trunk-merge/"
LISTED_PATHS_CAP = 10
UNKNOWN_EXIT = 2


class Kind(Enum):
    WEBUI = "webui"
    ENGINE = "engine"
    DESKTOP = "desktop"
    PYTHON = "python"
    HTTP_CONTRACT = "http-contract"
    LEDGER = "ledger"
    INFRA = "infra"
    PROSE = "prose"
    UNCLASSIFIED = "unclassified"


ALL_SEAMS: frozenset[str] = frozenset({"webui", "engine", "desktop", "python"})

#: Kinds that are not a seam themselves -> the seams they select.
_SELECTS: Mapping[Kind, frozenset[str]] = {
    Kind.WEBUI: frozenset({"webui"}),
    Kind.ENGINE: frozenset({"engine"}),
    Kind.DESKTOP: frozenset({"desktop"}),
    Kind.PYTHON: frozenset({"python"}),
    Kind.HTTP_CONTRACT: frozenset({"webui", "python"}),
    Kind.LEDGER: frozenset({"python"}),
    Kind.PROSE: frozenset(),
}
_NEVER_NARROWED = (Kind.INFRA, Kind.UNCLASSIFIED)

_PREFIXES: tuple[tuple[str, Kind], ...] = (
    ("apps/webui/frontend/", Kind.WEBUI),
    ("apps/audio-engine/", Kind.ENGINE),
    ("apps/desktop/", Kind.DESKTOP),
    ("apps/launcher/", Kind.DESKTOP),
    (".github/", Kind.INFRA),
    ("ci/", Kind.INFRA),
    (".ci/", Kind.INFRA),
    (".trunk/", Kind.INFRA),
    (".pnpm-store/", Kind.INFRA),
    # Trees whose only CI consumers are pytest modules.
    ("apps/", Kind.PYTHON),
    ("scripts/", Kind.PYTHON),
    ("tests/", Kind.PYTHON),
    ("ops/", Kind.PYTHON),
    ("tools/", Kind.PYTHON),
    ("research/", Kind.PYTHON),
    ("open-dj/", Kind.PYTHON),
    ("site/", Kind.PYTHON),
    ("usb-profiles/", Kind.PYTHON),
    ("reports/", Kind.PYTHON),
    ("data/", Kind.PYTHON),
    ("app_docs/", Kind.PYTHON),
    ("docs/", Kind.PYTHON),
    (".agents/", Kind.PYTHON),
    (".claude/", Kind.PYTHON),
    (".cursor/", Kind.PYTHON),
    (".codex/", Kind.PYTHON),
    (".learnings/", Kind.PYTHON),
    (".design-sync/", Kind.PYTHON),
)

_ROOT_FILES: Mapping[str, Kind] = {
    **dict.fromkeys(
        (
            "justfile",
            "Makefile",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            ".mergify.yml",
            ".gitignore",
            ".gitattributes",
            ".env.sample",
            ".coderabbit.yaml",
            ".pre-commit-config.yaml",
            ".gitleaks.toml",
            "osv-scanner.toml",
            "renovate.json",
            ".mcp.json",
            ".worktreeinclude",
        ),
        Kind.INFRA,
    ),
    **dict.fromkeys(
        (
            "pyproject.toml",
            "uv.lock",
            "conftest.py",
            "requirements.txt",
            "requirements-docs.txt",
            "requirements-ci.in",
            "requirements-release-check.in",
            "pylock.ci.toml",
            "pylock.docs.toml",
            "pylock.release-check.toml",
            ".python-version",
            ".importlinter",
            ".test_durations",
            "mkdocs.yml",
            "MANIFEST.in",
            "NOTICE",
            "LICENSE",
            ".mailmap",
            ".git-blame-ignore-revs",
            ".claudeignore",
            ".codexignore",
            ".cursorindexingignore",
            ".geminiignore",
            ".antigravityignore",
            ".tmp.hallo.cursor.txt",
        ),
        Kind.PYTHON,
    ),
    "reqs.json": Kind.LEDGER,
}

_LEDGER = ".planning/REQUIREMENTS.md"
_HTTP_CONTRACT = "apps/webui/openapi.json"
_WEBUI_TEST_SUFFIXES = (".ts", ".mjs", ".js", ".svelte")


@dataclass(frozen=True)
class SeamPlan:
    full: bool
    seams: frozenset[str]
    reason: str

    def as_json(self) -> str:
        return json.dumps({"full": self.full, "seams": sorted(self.seams), "reason": self.reason})


@dataclass(frozen=True)
class SeamTraffic:
    prs: int
    code_prs: int
    crossing_prs: int
    ledger_crossings: int
    touched: dict[str, int] = field(default_factory=dict)
    alone: dict[str, int] = field(default_factory=dict)


# ----- classify ---------------------------------------------------------------


def _is_ci_yml_prose(path: str) -> bool:
    paths, paths_ignore = IN_RUN_PULL_REQUEST_SCOPES["ci.yml"]
    return not workflow_would_run_for_files([path], paths=paths, paths_ignore=paths_ignore)


def classify(path: str) -> Kind:
    """The one kind `path` belongs to. Prose is read from ci.yml's own scope list, so the
    two readers of "docs-only" cannot disagree."""
    if path == _LEDGER:
        return Kind.LEDGER
    if path == _HTTP_CONTRACT:
        return Kind.HTTP_CONTRACT
    if _is_ci_yml_prose(path):
        return Kind.PROSE
    if path.startswith("tests/") and path.endswith(_WEBUI_TEST_SUFFIXES):
        return Kind.WEBUI
    if "/" not in path:
        return _ROOT_FILES.get(path, Kind.UNCLASSIFIED)
    return next((kind for prefix, kind in _PREFIXES if path.startswith(prefix)), Kind.UNCLASSIFIED)


# ----- plan -------------------------------------------------------------------


def _listed(paths: Sequence[str]) -> str:
    more = len(paths) - LISTED_PATHS_CAP
    return ", ".join(paths[:LISTED_PATHS_CAP]) + (f" and {more} more" if more > 0 else "")


def plan_seams(event_name: str, head_ref: str, changed_files: Sequence[str]) -> SeamPlan:
    """The seams a change set touches, or every seam when it must not narrow."""
    if event_name != "pull_request":
        return SeamPlan(True, ALL_SEAMS, f"event {event_name!r} is never narrowed")
    if head_ref.startswith(QUEUE_DRAFT_PREFIX):
        return SeamPlan(True, ALL_SEAMS, f"{head_ref} is a Trunk queue draft")
    if not changed_files:
        return SeamPlan(True, ALL_SEAMS, "the merge commit changes no files; unmeasured, so full")
    kinds = {path: classify(path) for path in changed_files}
    blocking = [path for path, kind in kinds.items() if kind in _NEVER_NARROWED]
    if blocking:
        return SeamPlan(True, ALL_SEAMS, f"never narrowed for: {_listed(blocking)}")
    seams = frozenset().union(*(_SELECTS[kind] for kind in kinds.values()))
    return SeamPlan(False, seams, f"{len(changed_files)} file(s) touch {sorted(seams) or 'no seam'}")


# ----- traffic ----------------------------------------------------------------


def _seams_of(kinds: Iterable[Kind]) -> frozenset[str]:
    return frozenset().union(*(_SELECTS.get(kind, frozenset()) for kind in kinds))


def seam_traffic(pr_files: Iterable[Sequence[str]]) -> SeamTraffic:
    """Crossing rate over merged PRs, one changed-file list each; raises on any unclassified
    path rather than quoting a rate over a corpus it cannot fully read."""
    rows = list(pr_files)
    unclassified = sorted({path for files in rows for path in files if classify(path) is Kind.UNCLASSIFIED})
    if unclassified:
        raise ValueError(f"{len(unclassified)} unclassified path(s): {_listed(unclassified)}")
    kind_sets = [{classify(path) for path in files} for files in rows]
    touched_sets = [_seams_of(kinds) for kinds in kind_sets]
    code = [seams for seams in touched_sets if seams]
    # A crossing that exists only because the ledger selects python: under a repository
    # split it is still a two-repository change, so it counts, but it is reported apart.
    ledger_crossings = sum(
        1
        for kinds, seams in zip(kind_sets, touched_sets, strict=True)
        if len(seams) > 1 and len(_seams_of(kinds - {Kind.LEDGER})) < 2
    )
    order = sorted(ALL_SEAMS)
    return SeamTraffic(
        prs=len(rows),
        code_prs=len(code),
        crossing_prs=sum(1 for seams in code if len(seams) > 1),
        ledger_crossings=ledger_crossings,
        touched={seam: sum(1 for seams in code if seam in seams) for seam in order},
        alone={seam: sum(1 for seams in code if seams == {seam}) for seam in order},
    )


# ----- CLI --------------------------------------------------------------------


def _run_shadow() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    if not event_name:
        raise RuntimeError("$GITHUB_EVENT_NAME is unset; this runs inside a GitHub Actions job")
    head_ref = os.environ.get("GITHUB_HEAD_REF", "")
    changed = merge_commit_changed_files(Path.cwd()) if event_name == "pull_request" else []
    plan = plan_seams(event_name, head_ref, changed)
    print(f"[ci-seams] shadow (nothing is skipped) plan={plan.as_json()}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as out:
            out.write(f"### Seam plan (shadow)\n\n- full: {plan.full}\n")
            out.write(f"- seams: {', '.join(sorted(plan.seams)) or 'none'}\n- {plan.reason}\n")
    return 0


def _run_baseline(corpus: Path) -> int:
    try:
        prs = json.loads(corpus.read_text(encoding="utf-8"))
        traffic = seam_traffic([pr["files"] for pr in prs])
    except ValueError as exc:
        print(f"UNKNOWN: {exc}")
        return UNKNOWN_EXIT
    if not traffic.code_prs:
        print(f"UNKNOWN: {traffic.prs} PR(s), none touches a code seam; no rate to report")
        return UNKNOWN_EXIT
    rate = traffic.crossing_prs / traffic.code_prs
    print(f"denominator: {traffic.prs} PRs, {traffic.code_prs} touch a code seam")
    print(f"crossing: {traffic.crossing_prs} of {traffic.code_prs} code PRs ({rate:.1%})")
    print(f"  of which {traffic.ledger_crossings} cross only through the requirements ledger")
    for seam in sorted(ALL_SEAMS):
        print(f"  {seam:8} touched by {traffic.touched[seam]:4}, alone in {traffic.alone[seam]:4}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("shadow", help="log this run's seam plan; skips nothing")
    baseline = commands.add_parser("baseline", help="crossing rate over a {number, files} JSON list")
    baseline.add_argument("corpus", type=Path)
    args = parser.parse_args(argv)
    if args.command == "shadow":
        return _run_shadow()
    if args.command == "baseline":
        return _run_baseline(args.corpus)
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
