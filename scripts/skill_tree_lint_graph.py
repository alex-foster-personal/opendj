"""Skill and file reference graphs for skill_tree_lint."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from scripts.export_global_skills import BARE_NAME_MIN_LEN, WIKI_RE, is_markdown
from scripts.skill_tree_lint_config import N, Skill
from scripts.skill_tree_lint_io import _read
from scripts.skill_tree_lint_markdown import _md_tokens, _parse_file


def _in_skill(target: Path, skill: Skill) -> bool:
    """Whether a path is one of this skill's own files: inside its directory
    and not inside a skill nested there. A nested skill's files are its own,
    and a link to it is a handoff, measured by K-03 rather than as a file.

    Containment is checked first, so nothing outside the skill is stat()ed."""
    if not target.is_relative_to(skill.root):
        return False
    return not any(
        (d / "SKILL.md").is_file() for d in target.parents if d != skill.root and d.is_relative_to(skill.root)
    )


def _file_edges(path: Path, skill: Skill, repo: Path) -> set[Path]:
    edges: set[Path] = set()
    for token in _md_tokens(path):
        for base in (path.parent, repo):
            target = (base / token).resolve()
            # Containment first: a quoted absolute path (a home directory)
            # must never be stat()ed, or an unreadable one aborts the scan.
            inside = target != path and _in_skill(target, skill)
            if inside and target.is_file():
                edges.add(target)
                break
    return edges


def _reachable(start: N, graph: dict[N, set[N]]) -> set[N]:
    seen: set[N] = set()
    stack = list(graph.get(start, ()))
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(graph.get(node, ()))
    return seen


def _components(graph: dict[N, set[N]]) -> dict[N, frozenset[N]]:
    """Each node's strongly connected group: the nodes it reaches that reach it back."""
    nodes = set(graph) | {t for targets in graph.values() for t in targets}
    reach: dict[N, set[N]] = {n: _reachable(n, graph) for n in nodes}
    groups: dict[N, frozenset[N]] = {}
    for node in nodes:
        mutual = {m for m in reach[node] if node in reach[m]}
        mutual.add(node)
        groups[node] = frozenset(mutual)
    return groups


def _deepest(graph: dict[N, set[N]], starts: Iterable[N]) -> list[N]:
    """Deepest chain from any start, with each cycle counted once at its full size.

    Cycles are collapsed first and the longest path is taken over the acyclic
    graph that leaves, a cycle contributing all its members (an upper bound on
    any simple path through it). Enumerating simple paths instead is factorial
    on a dense cycle, which a ring of cross-referencing skills would hit.
    """
    comp = _components(graph)
    memo: dict[frozenset[N], list[N]] = {}

    def best(group: frozenset[N]) -> list[N]:
        if group not in memo:
            after = {comp[t] for n in group for t in graph.get(n, ()) if comp[t] != group}
            tails = [best(g) for g in sorted(after, key=lambda g: sorted(map(str, g)))]
            memo[group] = sorted(group, key=str) + max(tails, key=len, default=[])
        return memo[group]

    chains: list[list[N]] = []
    for start in starts:
        chain: list[N] = best(comp[start])
        if start in chain:
            chain = [start] + [n for n in chain if n != start]
        chains.append(chain)
    return max(chains, key=len)


def _deep_edges(graph: dict[N, set[N]], start: N) -> int:
    """References made below the first level: every edge leaving a node that
    start reaches, other than start itself.

    A flat tree has none. Each reference added past the first level adds one,
    including one that ends at a node a shorter route already reaches, so a
    new deep route moves this even when no depth or node count changes.
    """
    return sum(len(graph.get(node, ())) for node in _reachable(start, graph) - {start})


def _file_graph(skill: Skill, repo: Path) -> dict[Path, set[Path]]:
    """The Markdown files SKILL.md reaches, each mapped to the files it references.

    Built outward from SKILL.md, so a note nothing links to is never read: it
    cannot be loaded, and an undecodable one must not abort the scan."""
    start = skill.skill_md.resolve()
    graph: dict[Path, set[Path]] = {}
    stack = [start]
    while stack:
        md = stack.pop()
        if md not in graph:
            graph[md] = _file_edges(md, skill, repo)
            stack.extend(t for t in graph[md] if is_markdown(t) and t not in graph)
    return graph


def file_ref_depth(skill: Skill, repo: Path) -> list[Path]:
    return _deepest(_file_graph(skill, repo), [skill.skill_md.resolve()])


def loaded_files(skill: Skill, repo: Path) -> list[Path]:
    """SKILL.md and every file it reaches: what an agent can actually be led to read.

    An unlinked note in the skill directory is never loaded, so it must not
    add skill references (and through them depth or cycles) either.
    """
    start = skill.skill_md.resolve()
    return sorted({start} | _reachable(start, _file_graph(skill, repo)))


# ----- K-03: skills naming skills -------------------------------------------


# A skill name ends where the name does: `x.md` and `x-old` are other things,
# while sentence punctuation (`see x.`) and a path separator are not.
_NAME_END = r"(?![\w-]|\.\w)"


def _invocation_pattern(name: str) -> re.Pattern[str]:
    """A `/name`, `$name` or `skill:name` call: how a skill is invoked by name."""
    return re.compile(rf"(?:(?<![\w/.:-])[/$]|(?<![\w-])(?i:skill):){re.escape(name)}{_NAME_END}")


def _name_patterns(name: str) -> list[re.Pattern[str]]:
    escaped = re.escape(name)
    patterns = [
        re.compile(rf"skills/{escaped}{_NAME_END}"),
        # Includes `skill:html`, the explicit form the exporter also reads.
        _invocation_pattern(name),
        # A sibling link such as [specialist](../html/SKILL.md): the label
        # need not name the skill, and the path has no skills/ segment.
        re.compile(rf"(?<![\w.])\.\./{escaped}{_NAME_END}"),
    ]
    if "-" in name and len(name) >= BARE_NAME_MIN_LEN:
        patterns.append(re.compile(rf"(?<![\w/$-]){escaped}(?![\w/-]|\.\w)"))
    return patterns


def _path_pattern(name: str) -> re.Pattern[str]:
    """A path token with this name as a directory: `(private path)`, `../x`,
    `../_vendored/x`, or a direct child `x/SKILL.md`. Any parent will do,
    because `_path_target` keeps only a token that resolves to a discovered
    skill directory. A bare word is not a path, so `x` alone needs a `/`."""
    escaped = re.escape(name)
    return re.compile(rf"(?<![\w./-])(?:[\w./-]*?/{escaped}{_NAME_END}|{escaped}(?=/))")


def _read_by_name(token: str, name: str) -> bool:
    """Whether a whole path token that resolves nowhere still names its skill.

    Only the shorthands do: `skills/x`, and `x/...` with no parent. Any other
    parent says where the skill lives, so `other-repo/skills/x` names
    nothing when it is not there."""
    return token[: -len(name)].rstrip("/") in ("", "skills")


def _path_target(token: str, md: Path, repo: Path, roots: list[Path]) -> Path | None:
    """The one skill directory a path token points at, or None if it is not one."""
    for base in (md.parent, repo):
        target = (base / token).resolve()
        if target in roots:
            return target
    return None


def _dest_skill(dest: str, md: Path, repo: Path, roots: set[Path]) -> Path | None:
    """The skill directory a whole link destination lies in, or None.

    The destination is resolved as written, whatever characters its parents
    hold (`<../some folder/x/SKILL.md>`), so a link to a skill's `SKILL.md`,
    to the directory itself, or to a file inside it all lead to that skill.
    A base under which the destination exists wins: `(private path)` is
    read from the repo root, not as a missing file in this skill's own
    directory. Where it exists under neither, another skill wins over this
    file's own, so a link to a missing file in another skill is still a
    handoff rather than a self-reference. Otherwise this file's directory is
    tried first, as a link is."""
    own = next((p for p in md.resolve().parents if p in roots), None)
    found: list[Path] = []
    for base in (md.parent, repo):
        target = (base / dest).resolve()
        root = next((p for p in (target, *target.parents) if p in roots), None)
        if root is not None and target.exists():
            return root
        if root is not None:
            found.append(root)
    return next((r for r in found if r != own), found[0] if found else None)


def _resolved_names(dest: str, md: Path, repo: Path, targets: dict[str, _Target]) -> set[str]:
    """The skill names whose directory a link destination points at."""
    whole = _dest_skill(dest, md, repo, {root for t in targets.values() for root in t.roots})
    # Only where the destination resolves, so a segment it climbs back out
    # of (`../a/../b/SKILL.md`) is not mistaken for where it points.
    return {name for name, t in targets.items() if whole in t.roots}


@dataclass(frozen=True)
class _Target:
    """Everything needed to find references to one skill name."""

    patterns: list[re.Pattern[str]]
    path: re.Pattern[str]
    roots: list[Path]


def _skill_edges(skill: Skill, targets: dict[str, _Target], repo: Path) -> set[Path]:
    """The skill directories one skill refers to.

    A path that resolves to one directory refers to that copy only. A bare
    name, wiki link or command cannot say which copy it means when two trees
    hold the same name, so it refers to every copy of that name.
    """
    by_path: set[Path] = set()
    by_name: set[str] = set()
    invokes_self = False
    invocation = _invocation_pattern(skill.name)
    all_roots = {root for t in targets.values() for root in t.roots}
    for md in loaded_files(skill, repo):
        # The parsed prose and decoded destinations, never the raw text: a path
        # written `../target%2Dskill/SKILL.md` still names `target-skill`, and a
        # name inside a link title is metadata, not a handoff.
        destinations, prose, labels, spans = _parse_file(md)
        # A label naming the skill its link resolves to only restates that path,
        # so it must not also name every copy: `[dup](../dup/SKILL.md)`. The rest
        # of the label is still read: `[a, then /b](../a/SKILL.md)` names b.
        # A link destination is a path whatever it looks like, so one that
        # resolves into a skill directory is a reference to it, with no `/` in
        # it (`[specialist](pdf)`, `[specialist](pdf#usage)`) or with parents
        # no path pattern spells (`<../some folder/pdf/SKILL.md>`).
        # So a destination is read only by where it resolves, never by its
        # spelling: `../../other-repo/skills/x/SKILL.md` names no local skill.
        by_path |= {r for d in destinations if (r := _dest_skill(d, md, repo, all_roots)) is not None}
        lines = [(prose, set[str](), False)]
        lines += [(label, _resolved_names(dest, md, repo, targets), False) for dest, label in labels]
        # A delimited path is taken whole, so it says where it points.
        lines += [(span, _resolved_names(dest, md, repo, targets) if dest else set(), True) for dest, span in spans]
        for line, restated, whole in lines:
            wiki = {m.group(1) for m in WIKI_RE.finditer(line)}
            invokes_self = invokes_self or skill.name in wiki or bool(invocation.search(line))
            for name, target in targets.items():
                rest = line
                # A path anchored at this file (`./x`, `../x`) says exactly where
                # it points, so one that resolves nowhere names nothing:
                # `../../other-repo/skills/x`. So does a whole delimited one
                # with any parent but the `skills/` shorthand. An unanchored
                # `skills/x` or a `/x` command is still read by name, and so
                # is any path in prose, where nothing says where it starts.
                for m in reversed(list(target.path.finditer(line))):
                    root = _path_target(m.group(0), md, repo, target.roots)
                    if root is not None:
                        by_path.add(root)
                    if (
                        root is not None
                        or m.group(0).startswith(("./", "../"))
                        or (whole and not _read_by_name(m.group(0), name))
                    ):
                        rest = rest[: m.start()] + " " + rest[m.end() :]
                if name not in restated and (name in wiki or any(p.search(rest) for p in target.patterns)):
                    by_name.add(name)
    # A skill names itself in its frontmatter, headings and paths to its own
    # files. Only an explicit invocation of itself is a handoff, a loop.
    if not invokes_self:
        by_name.discard(skill.name)
        by_path.discard(skill.root)
    return by_path | {root for name in by_name for root in targets[name].roots}


def _labels(skills: list[Skill], repo: Path) -> dict[Path, str]:
    """One graph node per skill DIRECTORY: a name shared by two trees keeps both."""
    counts = Counter(s.name for s in skills)
    return {s.root: s.name if counts[s.name] == 1 else s.root.relative_to(repo.resolve()).as_posix() for s in skills}


def skill_graph(skills: Iterable[Skill], repo: Path) -> dict[str, set[str]]:
    skills = list(skills)
    labels = _labels(skills, repo)
    roots: dict[str, list[Path]] = defaultdict(list)
    for s in skills:
        roots[s.name].append(s.root)
    targets = {name: _Target(_name_patterns(name), _path_pattern(name), rs) for name, rs in roots.items()}
    return {labels[s.root]: {labels[r] for r in _skill_edges(s, targets, repo)} for s in skills}


def _cycles(graph: dict[str, set[str]]) -> list[str]:
    """Each group of skills that can reach one another, at any cycle length."""
    groups = set(_components(graph).values())
    looped = {g for g in groups if len(g) > 1 or next(iter(g)) in graph.get(next(iter(g)), ())}
    return sorted(" <-> ".join(sorted(g)) if len(g) > 1 else f"{next(iter(g))} -> itself" for g in looped)


def _cyclic_refs(graph: dict[str, set[str]]) -> int:
    """References that sit on a cycle: both ends in one group that reaches itself.

    A skill invoking itself is a cycle of one, so its self-reference counts.

    This, not the number of groups, is what the gate ratchets. Adding a
    reference can merge two groups into one, so the group count can FALL
    while a new recursive handoff appears. Groups only ever merge as edges
    are added, so a reference inside one stays inside one: this count never
    drops when a reference is added, and rises when the new one closes a cycle.
    """
    comp = _components(graph)
    return sum(1 for src, targets in graph.items() for dst in targets if dst == src or comp[dst] == comp[src])
