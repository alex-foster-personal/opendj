"""PR-body ADR gate (issue #1489, widened by issue #2678, merge gate #3076).

A PR that touches an architecturally significant path must carry either
``ADR: NNNN`` naming a file under ``docs/decisions/``, ``ADR: NEW`` when
the PR adds ``ADR-NEW-<slug>.md``, or ``ADR: none, because <reason>`` with
a non-empty reason on the SAME line. Surrounding markdown backticks,
asterisks, or underscores on the marker are stripped before the
none/because matcher runs.
Independently of any gated path, ``docs/decisions/`` itself must never
carry two ``ADR-NNNN-*.md`` files sharing a number (issue #2678).

    python -m scripts.adr_check --pr 1234 --base origin/main
    python -m scripts.adr_check --base origin/main
    python -m scripts.adr_check --merge-base origin/main

Locally, before a PR exists, commit messages on ``base..HEAD`` stand in
for the body. Silence is the only thing that fails when a gated path
moved. An unknown id fails even if a ``none, because`` line is also
present, so a typo cannot hide behind the escape hatch.

Before any duplicate-id scan the CLI proves ``base`` is an ancestor of
``HEAD`` (or ``--merge-head`` in ``--merge-base`` mode); stale synthetic
PR merge refs fail with ``merge ref is stale, update your branch`` instead
of historical duplicate-id noise. The workflow ``edited`` trigger re-runs
body declarations; :mod:`scripts.adr_ref_freshness` handles merge refs.
A Trunk Merge Queue batch PR (head ``trunk-merge/pr-<N>/...``) carries only Trunk's
banner, so its body is the union of its member PRs' bodies; a batch whose members
cannot be read is UNKNOWN (exit 2).

What would satisfy this check without satisfying its intent, and why it
does not: a bare ``ADR: none`` with no ``because`` reason still fails --
the regex requires ``because`` and a non-whitespace reason on the SAME
LINE (horizontal whitespace only), so neither a rubber-stamped marker
nor the next section heading of an ordinary multiline body can silence
the gate for free. When a gated path moved but no declaration was
accepted, the gate distinguishes a missing declaration from a malformed
one and echoes the first offending ``ADR:`` line. ``gh pr view`` failing prints UNKNOWN and exits 2,
never a silent pass -- a failed read is not the same as a PR with
nothing to say. The duplicate-id check runs before the gated-path
early-return, so it fires on every invocation -- including a docs-only
PR and a bare run against ``main`` with an empty diff -- not only on a
PR that happens to touch a gated path.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.adr_pr_lookup import (
    DEFAULT_REPO,
    current_pr_number,
    pr_files,
    pr_view,
    trunk_batch_members,
)
from scripts.adr_ref_freshness import DEFAULT_GATED_DIR, check_ref_freshness, emit_gate_result

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ADR_DIR = REPO_ROOT / "docs" / "decisions"
DEFAULT_BASE = "origin/main"
DEEPEN_HINT = "git fetch --deepen=900 origin main"

# Prefixes: any path under these trees is gated.
GATED_PREFIXES: tuple[str, ...] = (
    "apps/cloud/",
    "apps/engine_core/",
    "apps/sync_hub/",
    "apps/webui/server/routes/",
    "apps/database/",
    ".github/workflows/",
)

# Globs: POSIX paths, matched with fnmatch.
GATED_GLOBS: tuple[str, ...] = (
    "apps/webui/server/state*",
    "apps/webui/server/**/state*",
    "*/migrations/*",
    ".planning/REQUIREMENTS.md",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
    "apps/webui/frontend/package.json",
    "apps/webui/openapi.json",
)

# Horizontal whitespace only. ``\s`` matches a newline, which is how a
# marker on its own line followed by ``## Tests`` would count as a reason.
_NONE_RE = re.compile(
    r"[`*_]*ADR:[ \t]*none[ \t]*,?[ \t]*because[`*_]*[ \t]+\S",
    re.IGNORECASE,
)
_ADR_LINE_RE = re.compile(r"ADR:", re.IGNORECASE)
_ID_RE = re.compile(
    r"ADR:[ \t]*(?:ADR-)?(\d{4})\b",
    re.IGNORECASE,
)
_NEW_RE = re.compile(
    r"ADR:[ \t]*NEW\b",
    re.IGNORECASE,
)
# Markdown emphasis/code markers stripped before the none/because matcher only.
_MARKDOWN_MARKER_RE = re.compile(r"[`*_]")
_ADR_CANDIDATE_RE = re.compile(r"ADR:", re.IGNORECASE)
_ADR_NUMERIC_FILENAME_RE = re.compile(r"ADR-(\d{4})-")
_ADR_NEW_FILENAME_RE = re.compile(r"ADR-NEW-(.+)\.md$")
_RENUMBERED_TO_RE = re.compile(
    r"superseded-number:.*?renumbered to ADR-(\d{4})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Verdict:
    """Result of one evaluation. ``code`` is the process exit code."""

    code: int
    message: str


def posix_path(path: str) -> str:
    rel = path.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def is_gated_path(path: str) -> bool:
    rel = posix_path(path)
    if any(rel == prefix.rstrip("/") or rel.startswith(prefix) for prefix in GATED_PREFIXES):
        return True
    return any(fnmatch.fnmatch(rel, pattern) for pattern in GATED_GLOBS)


def gated_paths(changed: list[str]) -> list[str]:
    return sorted({posix_path(p) for p in changed if is_gated_path(p)})


def existing_adr_ids(adr_dir: Path) -> set[str]:
    ids: set[str] = set()
    if not adr_dir.is_dir():
        return ids
    for path in adr_dir.glob("ADR-*.md"):
        match = _ADR_NUMERIC_FILENAME_RE.match(path.name)
        if match:
            ids.add(match.group(1))
    return ids


def pending_new_adr_paths(changed: list[str]) -> list[str]:
    return sorted(
        posix_path(p)
        for p in changed
        if posix_path(p).startswith("docs/decisions/ADR-NEW-") and p.endswith(".md")
    )


def duplicate_adr_ids_from_entries(entries: dict[str, int]) -> dict[str, list[str]]:
    """ADR id -> filenames for every numeric id claimed by 2+ files."""
    claimed: dict[str, list[str]] = {}
    for name, _size in sorted(entries.items()):
        if name.startswith("ADR-NEW-"):
            continue
        match = _ADR_NUMERIC_FILENAME_RE.match(name)
        if match:
            claimed.setdefault(match.group(1), []).append(name)
    return {adr_id: names for adr_id, names in claimed.items() if len(names) > 1}


def duplicate_new_slugs(entries: dict[str, int]) -> dict[str, list[str]]:
    claimed: dict[str, list[str]] = {}
    for name in sorted(entries):
        match = _ADR_NEW_FILENAME_RE.match(name)
        if match:
            claimed.setdefault(match.group(1), []).append(name)
    return {slug: names for slug, names in claimed.items() if len(names) > 1}


def duplicate_adr_ids(adr_dir: Path) -> dict[str, list[str]]:
    """ADR id -> filenames, for every id claimed by 2+ files. Empty when unique."""
    entries = {path.name: path.stat().st_size for path in sorted(adr_dir.glob("ADR-*.md"))}
    return duplicate_adr_ids_from_entries(entries)


def stale_renumber_violations(entries: dict[str, bytes]) -> list[str]:
    """Flag ADR files that still use an old id after a renumber banner."""
    violations: list[str] = []
    for name, content in sorted(entries.items()):
        match = _ADR_NUMERIC_FILENAME_RE.match(name)
        if not match:
            continue
        current_id = match.group(1)
        text = content.decode("utf-8", errors="replace")
        renumbered = _RENUMBERED_TO_RE.search(text)
        if not renumbered:
            continue
        target_id = renumbered.group(1)
        if target_id != current_id:
            violations.append(
                f"{name} claims renumbered to ADR-{target_id} but still uses ADR-{current_id}"
            )
    return violations


def normalize_adr_markers(text: str) -> str:
    """Strip markdown code/emphasis markers before the none/because matcher."""
    return _MARKDOWN_MARKER_RE.sub("", text or "")


def adr_candidate_lines(body: str) -> list[str]:
    """Raw body lines that look like they carry an ADR declaration."""
    return [line for line in (body or "").splitlines() if _ADR_CANDIDATE_RE.search(line)]


def ids_in_body(body: str) -> list[str]:
    return [match.group(1) for match in _ID_RE.finditer(body or "")]


def has_none_because(body: str) -> bool:
    return bool(_NONE_RE.search(normalize_adr_markers(body)))


def adr_declaration_lines(body: str) -> list[str]:
    return [line for line in (body or "").splitlines() if _ADR_LINE_RE.search(line)]


def has_adr_new(body: str) -> bool:
    return bool(_NEW_RE.search(body or ""))


def evaluate(
    changed: list[str],
    body: str,
    adr_dir: Path,
) -> Verdict:
    """Pure verdict over changed paths, a PR/commit body, and the ADR dir."""
    dupes = duplicate_adr_ids(adr_dir)
    if dupes:
        detail = "; ".join(
            f"ADR-{adr_id}: {', '.join(names)}" for adr_id, names in sorted(dupes.items())
        )
        return Verdict(1, f"[adr-check] duplicate ADR id(s) -- {detail}")

    hit = gated_paths(changed)
    if not hit:
        return Verdict(0, "[adr-check] OK -- no gated paths")

    known = existing_adr_ids(adr_dir)
    mentioned = ids_in_body(body)
    unknown = sorted({adr_id for adr_id in mentioned if adr_id not in known})
    if unknown:
        return Verdict(
            1,
            "[adr-check] unknown ADR id(s) "
            + ", ".join(f"ADR-{adr_id}" for adr_id in unknown)
            + f" (gated paths: {', '.join(hit)})",
        )

    valid = [adr_id for adr_id in mentioned if adr_id in known]
    if valid:
        shown = ", ".join(f"ADR-{adr_id}" for adr_id in valid)
        return Verdict(0, f"[adr-check] OK -- {shown} (gated paths: {', '.join(hit)})")

    new_paths = pending_new_adr_paths(changed)
    if has_adr_new(body) and new_paths:
        return Verdict(
            0,
            f"[adr-check] OK -- ADR: NEW ({', '.join(new_paths)}) (gated paths: {', '.join(hit)})",
        )

    if has_none_because(body):
        return Verdict(
            0,
            f"[adr-check] OK -- ADR: none with because-reason (gated paths: {', '.join(hit)})",
        )

    declarations = adr_declaration_lines(body)
    if declarations:
        return Verdict(
            1,
            "[adr-check] malformed ADR declaration: "
            + declarations[0]
            + f" (gated paths: {', '.join(hit)})",
        )

    return Verdict(
        1,
        "[adr-check] gated path(s) "
        + ", ".join(hit)
        + " with no ADR declaration found (expected ADR: <id> or "
        + "ADR: none, because <reason> on one line)",
    )


def evaluate_tree_entries(entries: dict[str, bytes]) -> Verdict:
    """Duplicate and stale checks over a virtual ADR directory."""
    sizes = {name: len(content) for name, content in entries.items()}
    dupes = duplicate_adr_ids_from_entries(sizes)
    if dupes:
        detail = "; ".join(
            f"ADR-{adr_id}: {', '.join(names)}" for adr_id, names in sorted(dupes.items())
        )
        return Verdict(1, f"[adr-check] duplicate ADR id(s) -- {detail}")

    new_dupes = duplicate_new_slugs(sizes)
    if new_dupes:
        detail = "; ".join(
            f"ADR-NEW-{slug}: {', '.join(names)}" for slug, names in sorted(new_dupes.items())
        )
        return Verdict(1, f"[adr-check] duplicate ADR-NEW slug(s) -- {detail}")

    stale = stale_renumber_violations(entries)
    if stale:
        return Verdict(1, "[adr-check] stale ADR renumber path(s) -- " + "; ".join(stale))

    return Verdict(0, "[adr-check] OK -- merged tree ADR ids unique")


def adr_paths_in_tree(repo_root: Path, tree_sha: str) -> dict[str, bytes]:
    prefix = "docs/decisions/"
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", tree_sha, "--", prefix],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-tree {tree_sha} failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    entries: dict[str, bytes] = {}
    for line in proc.stdout.splitlines():
        rel = line.strip()
        if not rel or not rel.startswith(prefix):
            continue
        filename = Path(rel).name
        if not filename.startswith("ADR-") or not filename.endswith(".md"):
            continue
        show = subprocess.run(
            ["git", "show", f"{tree_sha}:{rel}"],
            cwd=repo_root,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if show.returncode != 0:
            raise RuntimeError(
                f"git show {tree_sha}:{rel} failed rc={show.returncode}: {show.stderr.strip()}"
            )
        entries[filename] = show.stdout
    return entries


def merged_tree_sha(base_sha: str, head_sha: str, repo_root: Path) -> str:
    for sha in (base_sha, head_sha):
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if probe.returncode != 0:
            raise RuntimeError(f"commit {sha[:12]} is not present locally. Run: {DEEPEN_HINT}")

    proc = subprocess.run(
        ["git", "merge-tree", "--write-tree", base_sha, head_sha],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode > 1:
        raise RuntimeError(
            f"git merge-tree --write-tree failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    tree = proc.stdout.splitlines()[0].strip() if proc.stdout else ""
    if not tree:
        raise RuntimeError("git merge-tree --write-tree printed no tree oid")
    return tree


def evaluate_merged_tree(base_sha: str, head_sha: str, repo_root: Path) -> Verdict:
    tree = merged_tree_sha(base_sha, head_sha, repo_root)
    entries = adr_paths_in_tree(repo_root, tree)
    return evaluate_tree_entries(entries)


def git_changed(base: str, repo_root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git diff --name-only {base}...HEAD failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def git_commit_body(base: str, repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "log", "--format=%B", f"{base}..HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git log {base}..HEAD failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout


def git_rev_parse(ref: str, repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git rev-parse {ref} failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _resolve_changed_paths(
    args: argparse.Namespace,
    *,
    changed: list[str] | None,
    list_changed,
    repo_root: Path,
) -> list[str]:
    if changed is not None:
        return changed
    if args.changed_file:
        return list(args.changed_file)
    if args.pr is not None:
        return pr_files(args.pr, args.repo)
    if list_changed is not None:
        return list_changed(args.base, repo_root)
    return git_changed(args.base, repo_root)


def _resolve_body(
    args: argparse.Namespace,
    *,
    body: str | None,
    fetch,
    repo_root: Path,
) -> str:
    if body is not None:
        return body
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    pr_number = args.pr if args.pr is not None else current_pr_number(repo_root, args.repo)
    if pr_number is None:
        return git_commit_body(args.base, repo_root)
    pr = fetch(pr_number)
    members = trunk_batch_members(pr)
    if members is None:
        return pr.get("body") or ""
    # A Trunk batch carries every member's diff, so it carries every member's declaration.
    return "\n".join(fetch(member).get("body") or "" for member in members)


def main(
    argv: list[str] | None = None,
    *,
    fetch=pr_view,
    list_changed=None,
    changed: list[str] | None = None,
    body: str | None = None,
    repo_root: Path = REPO_ROOT,
    adr_dir: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="adr_check")
    parser.add_argument("--pr", type=int, default=None)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--merge-base", default=None)
    parser.add_argument("--merge-head", default="HEAD")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--body-file", default=None)
    parser.add_argument("--changed-file", action="append", default=None)
    parser.add_argument("--adr-dir", default=None)
    args = parser.parse_args(argv)

    decisions = Path(args.adr_dir) if args.adr_dir else (adr_dir or DEFAULT_ADR_DIR)

    if args.merge_base is not None:
        return _run_merge_base_mode(args, repo_root, decisions)

    try:
        paths = _resolve_changed_paths(
            args, changed=changed, list_changed=list_changed, repo_root=repo_root
        )
    except Exception as exc:
        print(f"[adr-check] UNKNOWN: could not list changed paths ({exc})", file=sys.stderr)
        return 2

    try:
        text = _resolve_body(args, body=body, fetch=fetch, repo_root=repo_root)
    except Exception as exc:
        print(f"[adr-check] UNKNOWN: could not read PR/commit body ({exc})", file=sys.stderr)
        return 2

    freshness = check_ref_freshness(
        args.base, "HEAD", repo_root, gated_dir=_gated_dir(decisions, repo_root)
    )
    if freshness is not None:
        return emit_gate_result(freshness.code, freshness.message)

    verdict = evaluate(paths, text, decisions)
    return emit_gate_result(verdict.code, verdict.message)


def _gated_dir(decisions: Path, repo_root: Path) -> str:
    """The ADR directory as a repo-relative path for the freshness guard; a
    directory outside the repository (test fixtures) falls back to the default."""
    try:
        return decisions.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return DEFAULT_GATED_DIR


def _run_merge_base_mode(args: argparse.Namespace, repo_root: Path, decisions: Path) -> int:
    freshness = check_ref_freshness(
        args.merge_base, args.merge_head, repo_root, gated_dir=_gated_dir(decisions, repo_root)
    )
    if freshness is not None:
        return emit_gate_result(freshness.code, freshness.message)
    try:
        base_sha = git_rev_parse(args.merge_base, repo_root)
        head_sha = git_rev_parse(args.merge_head, repo_root)
        verdict = evaluate_merged_tree(base_sha, head_sha, repo_root)
    except Exception as exc:
        print(f"[adr-check] UNKNOWN: merge-tree evaluation failed ({exc})", file=sys.stderr)
        return 2
    return emit_gate_result(verdict.code, verdict.message)


if __name__ == "__main__":
    raise SystemExit(main())
