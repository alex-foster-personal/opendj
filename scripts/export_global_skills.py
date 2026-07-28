#!/usr/bin/env python3
"""Export globally-defined agent skills into this repo before publishing.

Companion script for `.agents/skills/export-global-skills-before-publishing/SKILL.md`.

Problem: this repo's LOCAL skills (`.agents/skills/`, `.cursor/skills/`) reference
GLOBAL skills that live only on the author's machine (`~/.claude/skills/<name>/`)
or inside installed plugins (`superpowers:writing-skills`). A stranger who clones
the published repo has none of them, so every such reference is a dangling pointer.

This script finds those references, resolves them, proposes a per-dependency
triage, vendors the ones that should ship, rewrites the references to point at the
vendored copies, and then verifies the result loudly.

Requirements (mini-PRD). Statuses: -> out-of-scope, ? todo, OK done,
OK RAN done + ran-script + works-as-expected, OK RAN TESTED + regression tests.

R1 OK RAN  DISCOVERY: find every global-skill reference made by local skills and
           by repo docs (AGENTS.md, CLAUDE.md, root *.md, docs/), across BOTH the
           .agents/skills and .cursor/skills harness trees.
           [if a local skill contains `[[bifrost-remote-access]]` and it is not in
            the manifest then broken]
           [if a doc contains `~/.claude/skills/af-github-inline-clips` and it is
            not reported then broken]
           [if a bare word that happens to match a short global name like
            `template` is reported as high confidence then broken]
R2 OK RAN  MANIFEST: referencing file -> referenced name -> resolved source path
           -> exists yes/no, plus origin and proposed disposition.
           [if a reference resolves to nothing and `exists` is not false then broken]
           [if a plugin-namespaced name resolves without recording plugin+version
            then broken]
           [if a previous manifest set a disposition by hand and a re-run silently
            overwrites it then broken]
R3 OK RAN  TRIAGE: propose VENDOR / INLINE / DROP / BLOCKED per dependency.
           [if af-personal-details is proposed as anything but BLOCKED then broken]
           [if a source file containing a tailscale 100.x address is proposed
            VENDOR then broken]
           [if a domain skill referenced from inside a local skill is proposed DROP
            then broken]
R4 OK RAN  VENDORING: copy to `<dest-root>/_vendored/<original-name>/` with a
           provenance header (source, origin, sha256, weekday-stamped date,
           licence, snapshot warning) injected into SKILL.md only.
           [if the header cannot be stripped back to byte-identical source then broken]
           [if a plugin-sourced skill is vendored with no licence lookup attempt
            then broken]
           [if a source tree over --max-bytes is vendored silently then broken]
R5 OK RAN  REWRITING: `[[name]]` becomes a relative link to the vendored SKILL.md
           keeping the human-readable name, plus a machine-checkable marker.
           [if the rewritten link loses the original human-readable name then broken]
           [if a rewritten file has no `vendored-ref:` marker then broken]
           [if --write runs without --confirm-triage then broken]
R6 OK RAN  VERIFICATION: fails loudly (exit 1) on any unvendored global ref, sha
           mismatch, vendored BLOCKED skill, secret-pattern hit, or dangling marker.
           [if a vendored file is hand-edited after export and verify passes then broken]
           [if `_vendored/af-personal-details/` exists and verify passes then broken]
           [if a vendored file contains `AKIA0000000000000000` and verify passes
            then broken]
R7 OK RAN  DRIFT: `--check-drift` re-hashes every recorded source and exits 1 when
           any source changed or vanished since the export.
           [if the upstream skill gains a line and drift check passes then broken]
           [if a recorded source was deleted and drift check passes then broken]
           [if nothing changed and drift check fails then broken]
R8 -> Publishing the repo, history scrub, licence text authoring. See
      OSS-PUBLIC-PUSH-CHECKLIST.md; this script only handles skill vendoring.

Exit codes: 0 clean, 1 verification/drift/publish-blocker failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

# ----- config -----------------------------------------------------------------

USER_SKILLS_ROOT: Final[Path] = Path.home() / ".claude" / "skills"
PLUGIN_CACHE_ROOT: Final[Path] = Path.home() / ".claude" / "plugins" / "cache"
PLUGIN_MARKETPLACE_ROOT: Final[Path] = (
    Path.home() / ".claude" / "plugins" / "marketplaces"
)

VENDOR_DIRNAME: Final[str] = "_vendored"
MANIFEST_NAME: Final[str] = "MANIFEST.json"
MANIFEST_SCHEMA: Final[int] = 1

# Harness-scoped local skill trees. A published repo may need skills resolvable by
# Claude Code (.claude/skills), Cursor/Codex (.agents/skills, .cursor/skills), so
# discovery covers all of them and vendoring targets one dest-root at a time.
LOCAL_SKILL_TREES: Final[tuple[str, ...]] = (
    ".agents/skills",
    ".cursor/skills",
    ".claude/skills",
)

# Docs that commonly name a global skill in prose.
DOC_GLOBS: Final[tuple[str, ...]] = ("*.md", "docs/**/*.md", ".github/**/*.md")

SKIP_DIR_PARTS: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".tmp",
        "worktrees",
        VENDOR_DIRNAME,
    }
)

# Names that must NEVER be vendored: personal identity, machine addresses, or
# secrets pointers. Hard stop; the reference gets rewritten, never copied.
BLOCKED_NAMES: Final[dict[str, str]] = {
    "af-personal-details": "passport / DOB / address / personal identity",
    "af-show-user-secret-or-api-key": "secrets retrieval workflow",
    "af-get-verification-code": "OTP / verification-code interception",
    "bifrost-remote-access": "remote machine hostnames, tailscale addresses, SSH",
    "bifrost2-asset-store": "remote machine paths and credentials pointers",
    "doppler-secrets": "secrets-manager project/config names",
    "pw2agent": "password handoff protocol",
    "click-click-buy": "saved cards, checkout, identity autofill",
    "af-beeper": "personal messaging accounts and chat refs",
    "beeper-reply-triage": "personal messaging content",
    "kiki-send": "personal messaging identity",
    "kiki-personality": "verbatim personal voice samples",
    "cora-email": "personal mailbox",
    "agent-unblocks-via-maintainer-friend-on-beeper-or-email": "third-party personal contacts",
    "af-superhuman-cli-how-to": "personal mailbox tooling",
    "things3": "personal task database",
    "af-hdd-harddrive-mgmt": "machine-specific disk layout for one named Mac",
    "af-icloud-recover": "machine-specific iCloud state for one named Mac",
    "ram-cleanup": "machine-specific process/RAM layout",
    "macpro-backup-navigating": "machine-specific backup layout",
    "input-sharing": "named machine pair, IPs",
    "af-personal": "personal identity",
    "evidence-log": "personal legal matter",
    "negotiation-state": "personal legal matter",
}

# Personal-workflow prefixes: real skills, but about how ONE person drives their
# own harness. Useless to a public consumer -> propose DROP, not VENDOR.
PERSONAL_WORKFLOW_PREFIXES: Final[tuple[str, ...]] = (
    "af-",
    "gsd-",
    "gsd:",
    "cmux",
    "kiki-",
    "cc-",
    "claude-code-terminal-title",
)

# Content markers that make a source file unpublishable regardless of its name.
# Style borrowed from this repo's own OSS-PUBLIC-PUSH-CHECKLIST.md ("Secrets and
# private data") because the bifrost2 sync-kit referenced in the brief is not
# present in this tree (.planning/bifrost2-handoff/ holds only handoff markdown).
PERSONAL_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    (r"/Users/dev3?/", "absolute machine path (leaks username + layout)"),
    (r"\b100\.(?:\d{1,3})\.(?:\d{1,3})\.(?:\d{1,3})\b", "tailscale CGNAT address"),
    (r"\b(?:bifrost2?|demon-llama)\b", "named personal machine"),
    (r"seat-a|former-work-account", "personal account handle"),
    (r"\bpassport\b|\bdate of birth\b|\bDOB\b", "identity document field"),
    (r"\bdoppler\b", "secrets-manager reference"),
    (r"construct\s*>\s*dev_af|dev_personal", "secrets project/config name"),
)

SECRET_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key block"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "GitHub token"),
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "GitHub fine-grained PAT"),
    (r"\bsk-[A-Za-z0-9]{20,}\b", "OpenAI-style API key"),
    (r"\bsk-ant-[A-Za-z0-9-]{20,}\b", "Anthropic API key"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "Slack token"),
    (r"\bAIza[0-9A-Za-z_-]{35}\b", "Google API key"),
    (r"\bdp\.(?:pt|st|sa)\.[A-Za-z0-9]{10,}\b", "Doppler token"),
    (r"\bsntry[su]_[A-Za-z0-9]{20,}\b", "Sentry token"),
    (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", "JWT"),
    (
        r"(?:postgres(?:ql)?|mysql|redis|mongodb)://[^\s:@/]+:[^\s@]+@",
        "DSN with password",
    ),
    (r"\bspotify:track:[A-Za-z0-9]{15,}\b", "personal library track id"),
    (r"/Users/dev3?/", "absolute machine path"),
    (r"\b100\.(?:\d{1,3})\.(?:\d{1,3})\.(?:\d{1,3})\b", "tailscale CGNAT address"),
    (r"seat-a@|@gmail\.com", "personal email address"),
)

PROVENANCE_OPEN: Final[str] = "<!-- VENDORED-SKILL"
PROVENANCE_CLOSE: Final[str] = "-->"
PROVENANCE_RE: Final[re.Pattern[str]] = re.compile(
    r"<!-- VENDORED-SKILL\n.*?\n-->\n\n", re.DOTALL
)
VENDORED_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"<!-- vendored-ref: ([^\s>]+) -->"
)
# Escape hatches for documentation that merely SHOWS a reference shape.
EXAMPLES_BEGIN_RE: Final[re.Pattern[str]] = re.compile(r"<!-- ref-examples: begin -->")
EXAMPLES_END_RE: Final[re.Pattern[str]] = re.compile(r"<!-- ref-examples: end -->")
NOT_A_REF_RE: Final[re.Pattern[str]] = re.compile(r"<!-- not-a-ref -->")
DISPOSITION_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"<!-- (vendored-ref|inlined-global-ref|dropped-global-ref|blocked-global-ref): ([^\s>]+) -->"
)

MAX_VENDOR_BYTES_DEFAULT: Final[int] = 2_000_000

# ----- reference discovery ----------------------------------------------------

WIKI_RE: Final[re.Pattern[str]] = re.compile(r"\[\[([A-Za-z0-9][A-Za-z0-9:._/-]*)\]\]")
HOME_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"~/\.claude/skills/([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
)
SKILL_COLON_RE: Final[re.Pattern[str]] = re.compile(
    r"\bskill:([A-Za-z0-9](?:[A-Za-z0-9:._-]*[A-Za-z0-9])?)"
)
PLUGIN_NS_RE: Final[re.Pattern[str]] = re.compile(
    r"\b([a-z][a-z0-9-]{2,}(?::[a-z][a-z0-9-]{2,}){1,2})\b"
)
SLASH_CMD_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w/.-])/([a-z][a-z0-9-]{3,})\b"
)
BARE_NAME_MIN_LEN: Final[int] = 8


@dataclass(slots=True)
class Reference:
    """One discovered mention of a possibly-global skill."""

    referencing_file: str
    line: int
    raw: str
    kind: str
    name: str
    confidence: str


@dataclass(slots=True)
class Resolved:
    """A referenced name resolved (or not) to a source on this machine."""

    name: str
    source: Path | None
    origin: str
    plugin: str | None
    plugin_version: str | None
    licence_file: str | None
    exists: bool
    refs: list[Reference] = field(default_factory=list)
    disposition: str = "UNTRIAGED"
    reason: str = ""
    disposition_source: str = "auto"


# ----- helpers ----------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _today_stamp() -> str:
    """Weekday-carrying date, per the house rule (`Tue 28 Jul 2026`).

    Deliberately LOCAL time: the stamp is read by a human deciding whether an
    export is stale, so the operator's own calendar day is the right one.
    """
    return datetime.now(UTC).astimezone().strftime("%a %d %b %Y")


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _die(message: str, code: int = 2) -> None:
    print(f"[ERROR] {message}", file=sys.stderr)
    raise SystemExit(code)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _skipped(path: Path, root: Path) -> bool:
    return bool(SKIP_DIR_PARTS.intersection(path.relative_to(root).parts[:-1]))


def _tilde(path: Path) -> str:
    """Home-relative form. Never record an absolute machine path: it leaks the
    username into the published tree (see OSS-PUBLIC-PUSH-CHECKLIST.md) and breaks
    on every other machine."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _expand(recorded: str) -> Path:
    return Path(recorded).expanduser()


def _read_text(path: Path) -> str:
    """Read UTF-8 text. Binary or undecodable files are not reference sources."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


# ----- global skill index ----------------------------------------------------


def _index_user_skills() -> dict[str, Path]:
    """Map global user-scope skill name -> source file.

    Two real shapes on this machine: `~/.claude/skills/<name>/SKILL.md` (most) and
    a loose `~/.claude/skills/<name>.md` (e.g. explain-code.md).
    """
    if not USER_SKILLS_ROOT.is_dir():
        _die(f"global skills root missing: {USER_SKILLS_ROOT}")
    index: dict[str, Path] = {}
    for entry in sorted(USER_SKILLS_ROOT.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            skill_md = entry / "SKILL.md"
            if skill_md.is_file():
                index[entry.name] = skill_md
        elif entry.suffix == ".md":
            index[entry.stem] = entry
    return index


def _index_local_skills(repo: Path) -> dict[str, Path]:
    """Map local (in-repo) skill name -> SKILL.md, across every harness tree.

    Without this, a sibling local skill referenced as `[[other-local-skill]]` looks
    like an unresolvable global and gets flagged MISSING. Local refs need no export.
    """
    index: dict[str, Path] = {}
    for tree in LOCAL_SKILL_TREES:
        tree_path = repo / tree
        if not tree_path.is_dir():
            continue
        for skill_md in sorted(tree_path.rglob("SKILL.md")):
            if VENDOR_DIRNAME in skill_md.parts:
                continue
            index.setdefault(skill_md.parent.name, skill_md)
    return index


def _index_plugin_skills() -> dict[str, tuple[Path, str, str]]:
    """Map `plugin:skill` -> (SKILL.md, plugin, version).

    Real layout: `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md`
    and `~/.claude/plugins/marketplaces/<marketplace>/plugins/<plugin>/skills/<skill>/SKILL.md`.
    Version dirs are not always semver (git shas and `unknown` both occur), so the
    newest mtime wins rather than a version sort.
    """
    index: dict[str, tuple[Path, str, str]] = {}
    globs: tuple[tuple[Path, str], ...] = (
        (PLUGIN_CACHE_ROOT, "*/*/*/skills/*/SKILL.md"),
        (PLUGIN_CACHE_ROOT, "*/skills/*/SKILL.md"),
        (PLUGIN_MARKETPLACE_ROOT, "*/plugins/*/skills/*/SKILL.md"),
        (PLUGIN_MARKETPLACE_ROOT, "*/*/skills/*/SKILL.md"),
    )
    for root, pattern in globs:
        if not root.is_dir():
            continue
        for skill_md in root.glob(pattern):
            parts = skill_md.relative_to(root).parts
            skill_name = parts[-2]
            plugin = parts[1] if parts[1] != "skills" else parts[0]
            version = (
                parts[2] if len(parts) >= 5 and parts[2] != "skills" else "unversioned"
            )
            key = f"{plugin}:{skill_name}"
            previous = index.get(key)
            if (
                previous is None
                or skill_md.stat().st_mtime > previous[0].stat().st_mtime
            ):
                index[key] = (skill_md, plugin, version)
    return index


def _find_licence(source: Path) -> str | None:
    """Walk up from a source file looking for a licence file to attribute."""
    names = ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "LICENCE.md", "COPYING")
    for parent in list(source.parents)[:6]:
        for name in names:
            candidate = parent / name
            if candidate.is_file():
                return _tilde(candidate)
    return None


# ----- discovery -------------------------------------------------------------


def _scan_targets(repo: Path) -> list[Path]:
    targets: list[Path] = []
    for tree in LOCAL_SKILL_TREES:
        tree_path = repo / tree
        if not tree_path.is_dir():
            continue
        targets.extend(p for p in sorted(tree_path.rglob("*")) if p.is_file())
    for pattern in DOC_GLOBS:
        targets.extend(sorted(repo.glob(pattern)))
    for extra in ("AGENTS.md", "CLAUDE.md"):
        candidate = repo / extra
        if candidate.is_file():
            targets.append(candidate)
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in targets:
        if path in seen or _skipped(path, repo):
            continue
        if path.suffix.lower() not in {
            ".md",
            ".mdx",
            ".txt",
            ".yaml",
            ".yml",
            ".json",
            ".py",
            ".sh",
        }:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def _discover(
    repo: Path,
    targets: Sequence[Path],
    user_index: dict[str, Path],
    plugin_index: dict[str, tuple[Path, str, str]],
) -> list[Reference]:
    """Find every mention that could be a global-skill reference.

    Five reference kinds seen in the wild here, in descending confidence:
    wiki-link, home-path, skill-colon, plugin-namespaced, slash-command, bare name.
    Bare names are only reported when long and hyphenated, so short generic global
    names (`template`, `review`, `pdf`) cannot produce false positives.
    """
    bare_candidates = {
        name for name in user_index if "-" in name and len(name) >= BARE_NAME_MIN_LEN
    }
    references: list[Reference] = []
    skipped_doc_lines = 0
    for path in targets:
        text = _read_text(path)
        if not text:
            continue
        rel = _rel(path, repo)
        state = {"fence": False, "examples": False}
        for lineno, line in enumerate(text.splitlines(), start=1):
            # Documentation is not a reference. A skill that DOCUMENTS reference
            # shapes (this one does) would otherwise flood its own manifest with
            # its own examples, including non-skills like a bare wiki-link shape.
            if _is_doc_line(line, state):
                skipped_doc_lines += 1
                continue
            if DISPOSITION_MARKER_RE.search(line):
                continue
            for match in WIKI_RE.finditer(line):
                references.append(
                    Reference(
                        rel, lineno, match.group(0), "wiki-link", match.group(1), "high"
                    )
                )
            for match in HOME_PATH_RE.finditer(line):
                references.append(
                    Reference(
                        rel, lineno, match.group(0), "home-path", match.group(1), "high"
                    )
                )
            for match in SKILL_COLON_RE.finditer(line):
                references.append(
                    Reference(
                        rel,
                        lineno,
                        match.group(0),
                        "skill-colon",
                        match.group(1),
                        "high",
                    )
                )
            for match in PLUGIN_NS_RE.finditer(line):
                token = match.group(1)
                if token in plugin_index or token.split(":")[0] in {
                    key.split(":")[0] for key in plugin_index
                }:
                    references.append(
                        Reference(
                            rel, lineno, token, "plugin-namespaced", token, "high"
                        )
                    )
            for match in SLASH_CMD_RE.finditer(line):
                name = match.group(1)
                if name in user_index:
                    references.append(
                        Reference(
                            rel, lineno, match.group(0), "slash-command", name, "medium"
                        )
                    )
            for name in bare_candidates:
                if re.search(rf"(?<![\w/-]){re.escape(name)}(?![\w/-])", line):
                    references.append(
                        Reference(rel, lineno, name, "bare-name", name, "low")
                    )
    # Never silent: say how much was treated as documentation.
    print(
        f"discovery: skipped {skipped_doc_lines} line(s) as documentation "
        "(fenced code, ref-examples blocks, not-a-ref markers)"
    )
    return _dedupe(references)


def _dedupe(references: Iterable[Reference]) -> list[Reference]:
    rank = {"high": 0, "medium": 1, "low": 2}
    best: dict[tuple[str, int, str], Reference] = {}
    for ref in references:
        key = (ref.referencing_file, ref.line, ref.name)
        current = best.get(key)
        if current is None or rank[ref.confidence] < rank[current.confidence]:
            best[key] = ref
    return sorted(best.values(), key=lambda r: (r.referencing_file, r.line, r.name))


def _resolve_all(
    references: Sequence[Reference],
    local_index: dict[str, Path],
    user_index: dict[str, Path],
    plugin_index: dict[str, tuple[Path, str, str]],
) -> list[Resolved]:
    by_name: dict[str, Resolved] = {}
    for ref in references:
        entry = by_name.get(ref.name)
        if entry is None:
            entry = _resolve_one(ref.name, local_index, user_index, plugin_index)
            by_name[ref.name] = entry
        entry.refs.append(ref)
    return sorted(by_name.values(), key=lambda r: r.name)


def _resolve_one(
    name: str,
    local_index: dict[str, Path],
    user_index: dict[str, Path],
    plugin_index: dict[str, tuple[Path, str, str]],
) -> Resolved:
    if name in local_index:
        return Resolved(
            name=name,
            source=local_index[name],
            origin="local-in-repo",
            plugin=None,
            plugin_version=None,
            licence_file=None,
            exists=True,
        )
    if name in plugin_index:
        source, plugin, version = plugin_index[name]
        return Resolved(
            name=name,
            source=source,
            origin=f"plugin:{plugin}@{version}",
            plugin=plugin,
            plugin_version=version,
            licence_file=_find_licence(source),
            exists=True,
        )
    if ":" in name:
        tail = name.split(":")[-1]
        for key, (source, plugin, version) in plugin_index.items():
            if key.endswith(f":{tail}"):
                return Resolved(
                    name=name,
                    source=source,
                    origin=f"plugin:{plugin}@{version}",
                    plugin=plugin,
                    plugin_version=version,
                    licence_file=_find_licence(source),
                    exists=True,
                )
    if name in user_index:
        source = user_index[name]
        return Resolved(
            name=name,
            source=source,
            origin="user-global",
            plugin=None,
            plugin_version=None,
            licence_file=_find_licence(source),
            exists=True,
        )
    return Resolved(
        name=name,
        source=None,
        origin="unresolved",
        plugin=None,
        plugin_version=None,
        licence_file=None,
        exists=False,
    )


# ----- triage ----------------------------------------------------------------


def _scan_markers(text: str, patterns: Sequence[tuple[str, str]]) -> list[str]:
    hits: list[str] = []
    for pattern, label in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            hits.append(label)
    return hits


def _source_text(resolved: Resolved) -> str:
    if resolved.source is None:
        return ""
    root = resolved.source.parent
    chunks: list[str] = []
    for path in sorted(root.rglob("*")) if root.name != "skills" else [resolved.source]:
        if path.is_file() and path.suffix.lower() in {
            ".md",
            ".txt",
            ".py",
            ".sh",
            ".json",
            ".jsonl",
            ".yaml",
            ".yml",
        }:
            chunks.append(_read_text(path))
    if not chunks:
        chunks.append(_read_text(resolved.source))
    return "\n".join(chunks)


def _triage(resolved: Resolved, repo_skill_trees: Sequence[str]) -> None:
    """Assign a proposed disposition. Blocked wins over everything."""
    if resolved.origin == "local-in-repo":
        resolved.disposition = "LOCAL"
        resolved.reason = "already in-repo; ships with the clone, no export needed"
        return

    if not resolved.exists:
        resolved.disposition = "MISSING"
        resolved.reason = (
            "does not resolve on this machine; fix or delete the reference"
        )
        return

    blocked_reason = BLOCKED_NAMES.get(resolved.name) or BLOCKED_NAMES.get(
        resolved.name.split(":")[-1]
    )
    if blocked_reason:
        resolved.disposition = "BLOCKED"
        resolved.reason = f"blocklist: {blocked_reason}"
        return

    marker_hits = _scan_markers(_source_text(resolved), PERSONAL_MARKERS)
    if marker_hits:
        resolved.disposition = "BLOCKED"
        resolved.reason = "content markers: " + "; ".join(marker_hits)
        return

    from_skill_tree = any(
        ref.referencing_file.startswith(tree)
        for ref in resolved.refs
        for tree in repo_skill_trees
    )
    personal_workflow = resolved.name.startswith(PERSONAL_WORKFLOW_PREFIXES)

    if personal_workflow:
        resolved.disposition = "DROP"
        resolved.reason = "personal-workflow skill; irrelevant to a public consumer"
    elif from_skill_tree:
        resolved.disposition = "VENDOR"
        resolved.reason = "a local skill builds on it; a cloner needs the content"
    else:
        resolved.disposition = "INLINE"
        resolved.reason = "mentioned in docs only; a one-paragraph summary is cheaper"


def _apply_manifest_overrides(
    resolved: Sequence[Resolved], manifest_path: Path
) -> None:
    """A hand-edited disposition in an existing manifest wins over the proposal."""
    if not manifest_path.is_file():
        return
    previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    overrides = {
        entry["name"]: entry
        for entry in previous.get("dependencies", [])
        if entry.get("disposition_source") == "manifest-override"
    }
    valid = {"VENDOR", "INLINE", "DROP", "BLOCKED", "IGNORE"}
    for item in resolved:
        override = overrides.get(item.name)
        if override is None:
            continue
        if override["disposition"] not in valid:
            _die(
                f"manifest override for {item.name} has invalid disposition "
                f"{override['disposition']!r}; allowed: {sorted(valid)}"
            )
        if override["disposition"] == "VENDOR" and item.disposition == "BLOCKED":
            _die(
                f"manifest tries to VENDOR a BLOCKED skill: {item.name} ({item.reason}). "
                "BLOCKED is a hard stop; rewrite the reference instead."
            )
        item.disposition = override["disposition"]
        item.reason = override.get("reason", "manifest override")
        item.disposition_source = "manifest-override"


# ----- vendoring -------------------------------------------------------------


def _provenance_block(
    resolved: Resolved, source_sha: str, licence_vendored: bool
) -> str:
    licence = (
        resolved.licence_file
        or "UNKNOWN -- CHECK the upstream licence before publishing"
    )
    return "\n".join(
        [
            PROVENANCE_OPEN,
            f"original-name: {resolved.name}",
            f"source: {_tilde(resolved.source) if resolved.source else 'UNKNOWN'}",
            f"origin: {resolved.origin}",
            f"source-sha256: {source_sha}",
            f"exported: {_today_stamp()}",
            "exported-by: scripts/export_global_skills.py",
            f"licence-upstream: {licence}",
            f"licence-vendored: {'LICENSE.upstream' if licence_vendored else 'none'}",
            "snapshot: yes. The upstream skill keeps evolving; this copy will drift.",
            "  Re-run the export as a release step. Editing this copy makes it code",
            "  you own, so re-run its evals before shipping.",
            PROVENANCE_CLOSE,
            "",
            "",
        ]
    )


def _inject_provenance(text: str, block: str) -> str:
    """Insert the block after frontmatter so `name:`/`description:` stay parseable."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            cut = end + len("\n---\n")
            return text[:cut] + block + text[cut:]
    return block + text


def strip_provenance(text: str) -> str:
    """Inverse of _inject_provenance. Round-trip must be byte-exact."""
    return PROVENANCE_RE.sub("", text, count=1)


def _dir_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _vendor_one(
    resolved: Resolved, dest_root: Path, max_bytes: int
) -> dict[str, object]:
    if resolved.source is None:
        _die(f"cannot vendor unresolved skill: {resolved.name}")
    assert resolved.source is not None
    source_dir = resolved.source.parent
    loose_file = source_dir == USER_SKILLS_ROOT
    size = resolved.source.stat().st_size if loose_file else _dir_bytes(source_dir)
    if size > max_bytes:
        _die(
            f"{resolved.name}: source is {size} bytes, over --max-bytes {max_bytes}. "
            "Trim the source, raise the limit deliberately, or INLINE instead."
        )

    safe_name = resolved.name.replace(":", "__")
    dest = dest_root / safe_name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    files: dict[str, str] = {}
    if loose_file:
        shutil.copy2(resolved.source, dest / "SKILL.md")
    else:
        shutil.copytree(source_dir, dest, dirs_exist_ok=True)

    dest_skill = dest / "SKILL.md"
    if not dest_skill.is_file():
        _die(f"{resolved.name}: vendored copy has no SKILL.md at {dest_skill}")

    # Attribution: carry the upstream licence into the tree, do not just name it.
    licence_vendored = False
    if resolved.licence_file:
        licence_src = _expand(resolved.licence_file)
        if licence_src.is_file():
            shutil.copy2(licence_src, dest / "LICENSE.upstream")
            licence_vendored = True

    source_sha = _sha256_file(resolved.source)
    original = dest_skill.read_text(encoding="utf-8")
    injected = _inject_provenance(
        original, _provenance_block(resolved, source_sha, licence_vendored)
    )
    if strip_provenance(injected) != original:
        _die(
            f"{resolved.name}: provenance round-trip is not byte-exact; refusing to vendor"
        )
    dest_skill.write_text(injected, encoding="utf-8")

    for path in sorted(dest.rglob("*")):
        if path.is_file():
            files[_rel(path, dest)] = _sha256_file(path)

    return {
        "name": resolved.name,
        "vendored_dir": safe_name,
        "source": _tilde(resolved.source),
        "origin": resolved.origin,
        "plugin": resolved.plugin,
        "plugin_version": resolved.plugin_version,
        "licence_file": resolved.licence_file,
        "licence_vendored": "LICENSE.upstream" if licence_vendored else None,
        "source_sha256": source_sha,
        "exported": _today_stamp(),
        "exported_iso": _now_iso(),
        "files": files,
    }


# ----- reference rewriting ---------------------------------------------------


def _vendor_link(
    referencing_file: str, vendor_rel: str, name: str, safe_name: str
) -> str:
    """Relative markdown link that KEEPS the human-readable name, plus the marker."""
    target = posixpath.relpath(
        f"{vendor_rel}/{safe_name}/SKILL.md",
        start=posixpath.dirname(referencing_file) or ".",
    )
    return f"[{name}]({target}) <!-- vendored-ref: {name} -->"


def _is_doc_line(line: str, state: dict[str, bool]) -> bool:
    """Line-level documentation state machine, shared by discovery and rewriting.

    Both MUST agree: if discovery ignores a fenced example, the rewriter must not
    mangle it, and vice versa.
    """
    stripped = line.lstrip()
    if stripped.startswith(("```", "~~~")):
        state["fence"] = not state["fence"]
        return True
    if EXAMPLES_BEGIN_RE.search(line):
        state["examples"] = True
        return True
    if EXAMPLES_END_RE.search(line):
        state["examples"] = False
        return True
    return state["fence"] or state["examples"] or bool(NOT_A_REF_RE.search(line))


def _rewrite_file(
    path: Path,
    repo: Path,
    dispositions: dict[str, Resolved],
    vendor_rel: str,
    vendored_names: dict[str, str],
) -> tuple[str, int]:
    rel = _rel(path, repo)
    changes = 0

    def replace_wiki(match: re.Match[str]) -> str:
        nonlocal changes
        name = match.group(1)
        resolved = dispositions.get(name)
        if resolved is None or resolved.disposition in {"LOCAL", "IGNORE"}:
            return match.group(0)
        changes += 1
        if resolved.disposition == "VENDOR":
            return _vendor_link(rel, vendor_rel, name, vendored_names[name])
        if resolved.disposition == "INLINE":
            return f"`{name}` <!-- inlined-global-ref: {name} -->"
        if resolved.disposition == "BLOCKED":
            return f"`{name}` <!-- blocked-global-ref: {name} -->"
        return f"`{name}` <!-- dropped-global-ref: {name} -->"

    path_patterns: list[tuple[re.Pattern[str], str]] = []
    for name, resolved in dispositions.items():
        marker = {
            "VENDOR": f"vendored-ref: {name}",
            "INLINE": f"inlined-global-ref: {name}",
            "BLOCKED": f"blocked-global-ref: {name}",
            "DROP": f"dropped-global-ref: {name}",
        }.get(resolved.disposition)
        if marker is None:
            continue
        if resolved.disposition == "VENDOR":
            repl = _vendor_link(rel, vendor_rel, name, vendored_names[name])
        else:
            repl = f"`{name}` <!-- {marker} -->"
        patterns = [
            re.compile(rf"~/\.claude/skills/{re.escape(name)}(?:/SKILL\.md)?"),
            re.compile(rf"\bskill:{re.escape(name)}\b"),
        ]
        if ":" in name:
            patterns.append(re.compile(rf"(?<![\w:-]){re.escape(name)}(?![\w:-])"))
        else:
            patterns.append(re.compile(rf"(?<![\w/.:-])/{re.escape(name)}(?![\w-])"))
        path_patterns.extend((pattern, repl) for pattern in patterns)

    original = path.read_text(encoding="utf-8")
    state = {"fence": False, "examples": False}
    out: list[str] = []
    for line in original.splitlines(keepends=True):
        if _is_doc_line(line.rstrip("\n"), state) or DISPOSITION_MARKER_RE.search(line):
            out.append(line)
            continue
        line = WIKI_RE.sub(replace_wiki, line)
        for pattern, repl in path_patterns:
            line, count = pattern.subn(lambda _m, r=repl: r, line)
            changes += count
        out.append(line)
    return "".join(out), changes


# ----- verification ----------------------------------------------------------


def vendor_dir_rel(dest_root: Path, repo: Path, name: str) -> str:
    return _rel(dest_root / name, repo)


def _verify(repo: Path, dest_root: Path, strict_secrets: bool) -> list[str]:
    failures: list[str] = []
    manifest_path = dest_root / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"no manifest at {_rel(manifest_path, repo)}; run --write first"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    vendored = {entry["name"]: entry for entry in manifest.get("vendored", [])}
    dependencies = manifest.get("dependencies", [])

    # V1 no unvendored global refs left in any scanned file.
    local_index = _index_local_skills(repo)
    user_index = _index_user_skills()
    plugin_index = _index_plugin_skills()
    targets = _scan_targets(repo)
    live = _discover(repo, targets, user_index, plugin_index)
    for ref in live:
        # No `ref.name in vendored` shortcut: a vendored skill whose reference was
        # never rewritten is still a dangling pointer for a cloner. Discovery skips
        # lines that already carry a disposition marker, so anything reaching here
        # is genuinely unrewritten.
        if ref.confidence == "low" or ref.name in local_index:
            continue
        entry = next((d for d in dependencies if d["name"] == ref.name), None)
        if entry is None:
            failures.append(
                f"V1 unmanifested global ref: {ref.referencing_file}:{ref.line} -> {ref.name}"
            )
        elif entry["disposition"] == "IGNORE":
            continue
        elif entry["disposition"] in {"VENDOR", "MISSING", "UNTRIAGED"}:
            failures.append(
                f"V1 unvendored global ref still live: {ref.referencing_file}:{ref.line} "
                f"-> {ref.name} (disposition {entry['disposition']})"
            )

    # V2 every recorded file still hashes as recorded, and SKILL.md strips back to source.
    for name, entry in vendored.items():
        vdir = dest_root / entry["vendored_dir"]
        if not vdir.is_dir():
            failures.append(
                f"V2 manifest lists {name} but {_rel(vdir, repo)} is missing"
            )
            continue
        for rel_name, sha in entry["files"].items():
            fpath = vdir / rel_name
            if not fpath.is_file():
                failures.append(f"V2 missing vendored file: {_rel(fpath, repo)}")
            elif _sha256_file(fpath) != sha:
                failures.append(
                    f"V2 sha mismatch (hand-edited after export?): {_rel(fpath, repo)}"
                )
        skill_md = vdir / "SKILL.md"
        if skill_md.is_file():
            text = skill_md.read_text(encoding="utf-8")
            if PROVENANCE_OPEN not in text:
                failures.append(f"V2 no provenance header in {_rel(skill_md, repo)}")
            elif (
                _sha256_bytes(strip_provenance(text).encode("utf-8"))
                != entry["source_sha256"]
            ):
                failures.append(
                    f"V2 body does not strip back to recorded source sha: {_rel(skill_md, repo)}"
                )

    # V3 no BLOCKED skill got vendored, and no stray dir bypassed the manifest.
    vendored_dirs = (
        {p.name for p in dest_root.iterdir() if p.is_dir()}
        if dest_root.is_dir()
        else set()
    )
    for blocked_name, blocked_reason in BLOCKED_NAMES.items():
        safe = blocked_name.replace(":", "__")
        if safe in vendored_dirs or blocked_name in vendored:
            failures.append(
                f"V3 BLOCKED skill was vendored: {blocked_name} ({blocked_reason})"
            )
    for entry in dependencies:
        if entry["disposition"] != "BLOCKED":
            continue
        safe = entry["name"].replace(":", "__")
        if safe in vendored_dirs or entry["name"] in vendored:
            failures.append(
                f"V3 BLOCKED skill was vendored: {entry['name']} ({entry['reason']})"
            )
    known_dirs = {str(entry["vendored_dir"]) for entry in vendored.values()}
    for stray in sorted(vendored_dirs - known_dirs):
        failures.append(
            f"V3 stray vendored dir not in the manifest: {vendor_dir_rel(dest_root, repo, stray)}. "
            "Hand-copied skills bypass triage and the secret scan; re-run --write."
        )

    # V4 secret-pattern scan over the whole vendored set.
    for path in sorted(dest_root.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        hits = _scan_markers(_read_text(path), SECRET_PATTERNS)
        for hit in hits:
            failures.append(
                f"V4 secret pattern in vendored file: {_rel(path, repo)} -> {hit}"
            )
        if strict_secrets:
            for hit in _scan_markers(_read_text(path), PERSONAL_MARKERS):
                failures.append(
                    f"V4 personal marker in vendored file: {_rel(path, repo)} -> {hit}"
                )

    # V5 plugin-sourced skills need a real licence, never an assumed one.
    for name, entry in vendored.items():
        if not entry["origin"].startswith("plugin:"):
            continue
        if not entry.get("licence_file"):
            failures.append(
                f"V5 plugin-sourced {name} has no upstream licence recorded. A plugin skill "
                "may carry its own licence: CHECK the plugin repo, do not assume. Vendor the "
                "licence text or drop the dependency."
            )
        elif not (dest_root / entry["vendored_dir"] / "LICENSE.upstream").is_file():
            failures.append(
                f"V5 plugin-sourced {name} names a licence but LICENSE.upstream is not in "
                f"{_rel(dest_root / entry['vendored_dir'], repo)}; attribution is incomplete"
            )

    # V6 no dangling markers.
    for path in _scan_targets(repo):
        for match in VENDORED_REF_RE.finditer(_read_text(path)):
            name = match.group(1)
            entry = vendored.get(name)
            if (
                entry is None
                or not (dest_root / entry["vendored_dir"] / "SKILL.md").is_file()
            ):
                failures.append(
                    f"V6 dangling vendored-ref marker: {_rel(path, repo)} -> {name}"
                )
    return failures


def _check_drift(repo: Path, dest_root: Path) -> list[str]:
    manifest_path = dest_root / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"no manifest at {_rel(manifest_path, repo)}; nothing to drift-check"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    for entry in manifest.get("vendored", []):
        source = _expand(entry["source"])
        if not source.is_file():
            problems.append(
                f"DRIFT source vanished: {entry['name']} ({source}). Re-triage: the upstream "
                "skill was renamed, moved, or deleted."
            )
            continue
        if _sha256_file(source) != entry["source_sha256"]:
            problems.append(
                f"DRIFT source changed since {entry['exported']}: {entry['name']} ({source}). "
                "Re-run --write, diff the vendored copy, and re-run its evals if it is edited."
            )
    return problems


# ----- reporting -------------------------------------------------------------


def _print_table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> None:
    widths = [
        max(len(str(headers[i])), max((len(str(r[i])) for r in rows), default=0))
        for i in range(len(headers))
    ]
    bar = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    print(bar)
    print(
        "| " + " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)) + " |"
    )
    print(bar)
    for row in rows:
        print(
            "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)) + " |"
        )
    print(bar)


def _report(
    repo: Path, targets: Sequence[Path], resolved: Sequence[Resolved], dest_root: Path
) -> None:
    print(f"repo:        {repo}")
    print(f"dest-root:   {_rel(dest_root, repo)}")
    print(f"scanned:     {len(targets)} files")
    trees = [t for t in LOCAL_SKILL_TREES if (repo / t).is_dir()]
    print(f"skill trees: {', '.join(trees) if trees else 'NONE FOUND'}")
    print()
    if not resolved:
        print("dependency manifest: EMPTY -- no global-skill references found.")
        print("Nothing to vendor. Re-run after local skills land.")
        return
    rows = [
        [
            item.name,
            item.origin,
            "yes" if item.exists else "NO",
            item.disposition,
            f"{len(item.refs)}",
            item.reason[:58],
        ]
        for item in resolved
    ]
    _print_table(
        rows,
        ["referenced skill", "origin", "exists", "disposition", "refs", "reason"],
    )
    print()
    print("references:")
    for item in resolved:
        for ref in item.refs:
            print(
                f"  {ref.referencing_file}:{ref.line}  {ref.kind:<17} "
                f"[{ref.confidence}] {ref.raw}  ->  {item.disposition}"
            )


# ----- main ------------------------------------------------------------------


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="export_global_skills.py",
        description="Vendor globally-defined skills into this repo before publishing.",
    )
    parser.add_argument("--repo", default=".", help="repo root (default: cwd)")
    parser.add_argument(
        "--dest-root",
        default=".agents/skills",
        help="harness skill tree that receives _vendored/ (default: .agents/skills). "
        "Run once per harness that must resolve the skills.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="discover + triage, write nothing"
    )
    mode.add_argument(
        "--write", action="store_true", help="vendor, rewrite refs, write manifest"
    )
    mode.add_argument(
        "--verify", action="store_true", help="run the verification checks"
    )
    mode.add_argument(
        "--check-drift", action="store_true", help="have sources changed since export"
    )
    parser.add_argument(
        "--confirm-triage",
        action="store_true",
        help="required with --write; you have read the triage table and accept it",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=MAX_VENDOR_BYTES_DEFAULT,
        help=f"refuse to vendor a source tree larger than this (default {MAX_VENDOR_BYTES_DEFAULT})",
    )
    parser.add_argument(
        "--strict-secrets",
        action="store_true",
        help="treat personal markers (machine paths, handles) as verification failures too",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = _parse_args(argv)
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        _die(f"repo root is not a directory: {repo}")
    dest_tree = repo / args.dest_root
    if not dest_tree.is_dir():
        _die(
            f"dest-root does not exist: {dest_tree}. Create the harness skill tree first, "
            "or pass --dest-root for the harness you mean (.agents/skills, .cursor/skills, "
            ".claude/skills)."
        )
    dest_root = dest_tree / VENDOR_DIRNAME
    vendor_rel = _rel(dest_root, repo)

    if args.verify:
        failures = _verify(repo, dest_root, args.strict_secrets)
        if failures:
            print(
                f"[ERROR] verification FAILED with {len(failures)} problem(s):",
                file=sys.stderr,
            )
            for failure in failures:
                print(f"  {failure}", file=sys.stderr)
            return 1
        print(
            "[OK] verification passed: no unvendored global refs, all shas match, "
            "no BLOCKED skill vendored, no secret patterns."
        )
        return 0

    if args.check_drift:
        problems = _check_drift(repo, dest_root)
        if problems:
            print(
                f"[ERROR] drift detected in {len(problems)} vendored skill(s):",
                file=sys.stderr,
            )
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
        print("[OK] no drift: every vendored source still hashes as recorded.")
        return 0

    local_index = _index_local_skills(repo)
    user_index = _index_user_skills()
    plugin_index = _index_plugin_skills()
    targets = _scan_targets(repo)
    references = _discover(repo, targets, user_index, plugin_index)
    resolved = _resolve_all(references, local_index, user_index, plugin_index)
    trees = [t for t in LOCAL_SKILL_TREES if (repo / t).is_dir()]
    for item in resolved:
        _triage(item, trees)
    _apply_manifest_overrides(resolved, dest_root / MANIFEST_NAME)

    print(
        f"skill index: {len(local_index)} local in-repo, {len(user_index)} user-global, "
        f"{len(plugin_index)} plugin-scope"
    )
    print()
    _report(repo, targets, resolved, dest_root)

    blocked = [i for i in resolved if i.disposition == "BLOCKED"]
    missing = [i for i in resolved if i.disposition == "MISSING"]
    to_vendor = [i for i in resolved if i.disposition == "VENDOR"]

    print()
    print(
        f"summary: {len(to_vendor)} VENDOR, "
        f"{len([i for i in resolved if i.disposition == 'INLINE'])} INLINE, "
        f"{len([i for i in resolved if i.disposition == 'DROP'])} DROP, "
        f"{len(blocked)} BLOCKED, {len(missing)} MISSING, "
        f"{len([i for i in resolved if i.disposition == 'LOCAL'])} LOCAL (no action), "
        f"{len([i for i in resolved if i.disposition == 'IGNORE'])} IGNORE"
    )

    if args.dry_run:
        for item in blocked:
            print(f"[BLOCKED] {item.name}: {item.reason}")
            print("          Hard stop. Rewrite the reference; never vendor this.")
        for item in missing:
            print(
                f"[MISSING] {item.name}: referenced but unresolvable on this machine."
            )
        if blocked or missing:
            print()
            print(
                "[ERROR] publish blockers present. Resolve before --write.",
                file=sys.stderr,
            )
            return 1
        print("[OK] dry run clean. Re-run with --write --confirm-triage to export.")
        return 0

    if not args.confirm_triage:
        _die("--write needs --confirm-triage. Read the triage table above first.")
    if blocked:
        _die(
            f"{len(blocked)} BLOCKED dependency(ies): "
            + ", ".join(i.name for i in blocked)
            + ". Rewrite those references; BLOCKED is never vendored."
        )
    if missing:
        _die(
            f"{len(missing)} unresolvable reference(s): "
            + ", ".join(i.name for i in missing)
            + ". Fix or delete them."
        )

    dest_root.mkdir(parents=True, exist_ok=True)
    vendored_entries: list[dict[str, object]] = []
    vendored_names: dict[str, str] = {}
    for item in to_vendor:
        entry = _vendor_one(item, dest_root, args.max_bytes)
        vendored_entries.append(entry)
        vendored_names[item.name] = str(entry["vendored_dir"])
        print(f"[OK] vendored {item.name} -> {vendor_rel}/{entry['vendored_dir']}/")

    dispositions = {i.name: i for i in resolved}
    rewritten = 0
    for path in targets:
        if _rel(path, repo).startswith(vendor_rel):
            continue
        new_text, changes = _rewrite_file(
            path, repo, dispositions, vendor_rel, vendored_names
        )
        if changes:
            path.write_text(new_text, encoding="utf-8")
            rewritten += 1
            print(f"[OK] rewrote {changes} ref(s) in {_rel(path, repo)}")
    print(f"[OK] rewrote references in {rewritten} file(s)")

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "generated": _today_stamp(),
        "generated_iso": _now_iso(),
        "repo_root": repo.name,
        "dest_root": vendor_rel,
        "scan_roots": trees + list(DOC_GLOBS),
        "dependencies": [
            {
                "name": i.name,
                "origin": i.origin,
                "resolved_source": _tilde(i.source) if i.source else None,
                "exists": i.exists,
                "disposition": i.disposition,
                "disposition_source": i.disposition_source,
                "reason": i.reason,
                "references": [
                    {
                        "referencing_file": r.referencing_file,
                        "line": r.line,
                        "kind": r.kind,
                        "confidence": r.confidence,
                        "raw": r.raw,
                    }
                    for r in i.refs
                ],
            }
            for i in resolved
        ],
        "vendored": vendored_entries,
    }
    (dest_root / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(f"[OK] manifest written: {vendor_rel}/{MANIFEST_NAME}")

    failures = _verify(repo, dest_root, args.strict_secrets)
    if failures:
        print(
            f"[ERROR] post-export verification FAILED ({len(failures)}):",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("[OK] post-export verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
