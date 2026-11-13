"""skill_tree_lint.py -- keep agent skills short and their reference trees flat.

A skill is read by an agent in the middle of real work, so every line and every
hop costs context. Anthropic's skill authoring guidance
(platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices, read
Wed 23 Sep 2026) sets two numbers this file measures:

  * "Keep SKILL.md body under 500 lines for optimal performance. Split content
    into separate files when approaching this limit."
  * "Keep references one level deep from SKILL.md." A file reached through
    another referenced file may only be previewed (`head -100`), not read.

the maintainer asked for the same thing for skills that call other skills (Wed 23 Sep
2026): subskills are fine, a deep chain of them is not. So this measures three
trees, all from the repo's own skill directories:

Requirements (status key: -> out-of-scope, ? todo, OK done, RUN done+ran,
REG done+ran+regression-tested):

REG  K-01 Count SKILL.md files at or over CFG.SKILL_MAX_LINES lines (the
          rule is "under 500", so a 500-line file already breaks it).
          [if a SKILL.md grows to 500 lines then skills.over_line_limit rises]
          [if it is split into SKILL.md plus a referenced file then it falls]
          Also total the lines past the last allowed one across those files,
          so one already-long skill growing longer still moves a gated number.
          [if a 612-line SKILL.md grows to 700 then skills.over_line_excess
           rises by 88 while skills.over_line_limit stays put]
REG  K-02 Measure the deepest chain of markdown files one skill reaches inside
          its own directory, starting at SKILL.md (Anthropic's "one level").
          [if SKILL.md -> a.md -> b.md then skills.file_ref_depth_max is 2]
          [if SKILL.md links a.md and b.md directly then it is 1]
          A link destination is read whole, angle-bracketed or percent-encoded.
          [if SKILL.md links <reference guide.md> then that file is reached]
          Also count every reference made below the first level (each link
          out of a file SKILL.md reaches, other than SKILL.md itself), so a
          second deep chain moves a gated number even while the deepest one
          is unchanged, and so does a deep route to a file a shorter route
          already reaches.
          [if a skill already 3 deep adds SKILL.md -> c.md -> d.md then
           skills.file_ref_deep_edges rises by 1 while the max stays put]
          [if SKILL.md links a.md and c.md and a.md now links c.md too then
           skills.file_ref_deep_edges rises by 1]
          Every Markdown suffix the exporter recognizes (.md, .mdx, .mdc,
          .markdown, any case) is followed; a root-relative destination
          (/docs/x) is a web path, never a local file or skill.
          [if SKILL.md links guide.MDX then that file is reached]
          [if SKILL.md links /docs/skills/other-skill then it is not a ref]
REG  K-03 Measure the deepest chain of skills naming other skills (skill A
          names B, B names C = depth 2), and find the groups of skills that
          can reach one another (cycles of any length, not only pairs), which
          make "depth" unbounded for a reader. The gate ratchets the number of
          references on a cycle, which never falls when a reference is added
          (merging two cycles into one would lower a count of groups).
          [if A names B, B names C and C names A then one cycle is reported]
          A skill's own name turns up in its frontmatter, headings and paths to
          its own files, so only an explicit invocation of itself ([[name]],
          $name, /name) counts as a self-reference, which is a one-skill loop.
          [if a skill writes [[its-own-name]] then cyclic_skill_refs rises]
          A path reference resolves to the one skill directory it names; only a
          bare name reaches every tree's copy of a shared name.
          [if a skill links ../dup/SKILL.md then only that tree's dup is a ref]
          Only SKILL.md and the files it reaches (as K-02 walks them) are read,
          since an agent is never led to an unlinked note.
          [if an unlinked note names a skill then it is not a ref]
          [if a reference joins two cycles into one then the count rises]
          Depth collapses each cycle first, so a dense ring of skills costs
          polynomial time, not the factorial cost of enumerating simple paths.
          [if A names B and B names C then skills.skill_ref_depth_max is 2]
          Also count every handoff made below the first level from each
          skill, as K-02 does for files.
          [if a new chain D -> E -> F appears beside a deeper one then
           skills.skill_ref_deep_edges rises while the max stays put]
          [if a name appears only inside a fenced example then it is not a ref]
REG  K-04 Count agent rule files ((private path)/**/*.md) at or over CFG.RULE_MAX_LINES.
          Rules are meant to be small and single-topic; a long one is a doc.
          Total their lines past the last allowed one too, as K-01 does.
          [if a 250-line rule grows to 900 then skills.rules_over_excess
           rises by 650 while skills.rules_over_limit stays put]
REG  K-05 Refuse to report a clean scan over an empty skill set: zero is both
          a value and an error signature ((private path)).
          [if fewer than CFG.MIN_SKILLS skills are found then the run aborts]
          [if a SKILL.md git tracks under a skill tree is not discovered then
           the run aborts, so the scanned set cannot shrink unnoticed]

Skill references reuse scripts/export_global_skills.py's documentation filter
(fenced code, `ref-examples` blocks, `<!-- not-a-ref -->` lines) so the two
tools agree about what counts as a reference.

Usage (markdown-it-py comes from the pinned quality tool manifest, not the
project venv, so run it the way the quality gate does):
    UV="uv run --isolated --no-project --with-requirements ops/quality/requirements.txt"
    $UV python -m scripts.skill_tree_lint           # report every measurement
    $UV python -m scripts.skill_tree_lint --json    # the same, machine readable
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit  # re-exported for tests that monkeypatch skill_tree_lint.urlsplit

from scripts.export_global_skills import LOCAL_SKILL_TREES
from scripts.skill_tree_lint_config import CFG, Report, Skill
from scripts.skill_tree_lint_graph import (
    _cycles,
    _cyclic_refs,
    _deep_edges,
    _deepest,
    _file_graph,
    file_ref_depth,
    skill_graph,
)
from scripts.skill_tree_lint_io import _line_count


def discover(repo: Path) -> list[Skill]:
    """Every skill directory, once, even when one tree symlinks another."""
    seen: dict[Path, Skill] = {}
    for tree in LOCAL_SKILL_TREES:
        base = repo / tree
        if not base.is_dir():
            continue
        for skill_md in sorted(base.rglob("SKILL.md")):
            real = skill_md.parent.resolve()
            seen.setdefault(real, Skill(skill_md.parent.name, real))
    return sorted(seen.values(), key=lambda s: s.name)


def _over_limit(files: Iterable[tuple[str, Path]], limit: int) -> list[str]:
    """Files at or over an exclusive limit: "under 500" makes 500 a violation."""
    return [f"{label} ({n} lines)" for label, path in files if (n := _line_count(path)) >= limit]


def _excess_lines(files: Iterable[Path], limit: int) -> int:
    """Lines past the last allowed one (limit - 1), summed across files."""
    return sum(max(0, _line_count(path) - (limit - 1)) for path in files)


def _tracked_skill_dirs(repo: Path) -> set[Path]:
    """The resolved directory of every SKILL.md git tracks under a skill tree.

    The expected population, read from the index rather than the disk, so a
    skill the scan skips (a missing tree, a symlinked skill directory rglob
    does not enter) is a named failure instead of a smaller denominator."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH, so the expected skill set cannot be read")
    out = subprocess.run(
        [git, "-C", str(repo), "ls-files", "-z", "--", *LOCAL_SKILL_TREES],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    return {(repo / rel).parent.resolve() for rel in out.split("\0") if Path(rel).name == "SKILL.md"}


def measure(repo: Path) -> Report:
    skills = discover(repo)
    if len(skills) < CFG.MIN_SKILLS:
        raise RuntimeError(
            f"found {len(skills)} skills under {', '.join(LOCAL_SKILL_TREES)} in {repo}, "
            f"expected at least {CFG.MIN_SKILLS}. That is a broken scan, not a clean tree."
        )
    if missed := sorted(_tracked_skill_dirs(repo) - {s.root for s in skills}):
        raise RuntimeError(
            f"git tracks {len(missed)} SKILL.md the scan did not discover: "
            f"{', '.join(p.as_posix() for p in missed)}. That is a shrunken scan, not a clean tree."
        )
    worst_file = max((file_ref_depth(s, repo) for s in skills), key=len)
    deep_edges = sum(_deep_edges(_file_graph(s, repo), s.skill_md.resolve()) for s in skills)
    graph = skill_graph(skills, repo)
    worst_skill = _deepest(graph, sorted(graph))
    rules_dir = repo / CFG.RULES_DIR
    # Rules may sit in subdirectories ((private path)); every one loads.
    if not rules_dir.is_dir():
        # A missing directory would read as zero rules, a clean result the scan never measured.
        raise RuntimeError(f"skill_tree_lint: {rules_dir} is not a directory. That is a broken scan, not a clean tree.")
    rules = sorted(rules_dir.rglob("*.md"))
    return Report(
        skills_scanned=len(skills),
        over_line_limit=_over_limit(((s.name, s.skill_md) for s in skills), CFG.SKILL_MAX_LINES),
        over_line_excess=_excess_lines((s.skill_md for s in skills), CFG.SKILL_MAX_LINES),
        file_ref_depth_max=len(worst_file) - 1,
        file_ref_worst=[p.relative_to(repo.resolve()).as_posix() for p in worst_file],
        skill_ref_depth_max=len(worst_skill) - 1,
        skill_ref_worst=worst_skill,
        file_ref_deep_edges=deep_edges,
        skill_ref_deep_edges=sum(_deep_edges(graph, name) for name in graph),
        skill_ref_cycles=_cycles(graph),
        cyclic_skill_refs=_cyclic_refs(graph),
        rules_over_limit=_over_limit(((p.relative_to(rules_dir).as_posix(), p) for p in rules), CFG.RULE_MAX_LINES),
        rules_over_excess=_excess_lines(rules, CFG.RULE_MAX_LINES),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1] if __doc__ else None)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    report = measure(args.repo)
    if args.json:
        print(json.dumps(report.__dict__, indent=2))
        return 0
    print(f"skills scanned: {report.skills_scanned}")
    print(f"SKILL.md at or over {CFG.SKILL_MAX_LINES} lines: {report.over_line_limit or 'none'}")
    file_chain = " -> ".join(report.file_ref_worst)
    skill_chain = " -> ".join(report.skill_ref_worst)
    print(f"deepest file chain inside a skill: {report.file_ref_depth_max} ({file_chain})")
    print(f"deepest skill chain: {report.skill_ref_depth_max} ({skill_chain})")
    print(f"file references below the first level: {report.file_ref_deep_edges}")
    print(f"skill handoffs below the first level: {report.skill_ref_deep_edges}")
    print(f"skill cycles: {report.skill_ref_cycles or 'none'} ({report.cyclic_skill_refs} references on them)")
    print(f"rule files at or over {CFG.RULE_MAX_LINES} lines: {report.rules_over_limit or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# Re-exported: the public surface callers import from scripts.skill_tree_lint
from scripts.skill_tree_lint_config import (  # noqa: F401
    CFG,
    Report,
    Skill,
    UnparsableDestinationError,
)
from scripts.skill_tree_lint_graph import (  # noqa: F401
    file_ref_depth,
    loaded_files,
    skill_graph,
)

__all__ = [
    "CFG",
    "Report",
    "Skill",
    "UnparsableDestinationError",
    "discover",
    "file_ref_depth",
    "loaded_files",
    "main",
    "measure",
    "skill_graph",
    "urlsplit",
]
