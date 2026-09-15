"""PR-body ADR gate (issue #1489, widened by issue #2678).

A PR that touches an architecturally significant path must carry either
``ADR: NNNN`` naming a file under ``docs/decisions/``, or
``ADR: none, because <reason>`` with a non-empty reason on the SAME line.
Independently of any gated path, ``docs/decisions/`` itself must never
carry two ``ADR-NNNN-*.md`` files sharing a number (issue #2678).

    python -m scripts.adr_check --pr 1234 --base origin/main
    python -m scripts.adr_check --base origin/main

Locally, before a PR exists, commit messages on ``base..HEAD`` stand in
for the body. Silence is the only thing that fails when a gated path
moved. An unknown id fails even if a ``none, because`` line is also
present, so a typo cannot hide behind the escape hatch.

What would satisfy this check without satisfying its intent, and why it
does not: a bare ``ADR: none`` with no ``because`` reason still fails --
the regex requires ``because`` and a non-whitespace reason on the SAME
LINE (horizontal whitespace only), so neither a rubber-stamped marker
nor the next section heading of an ordinary multiline body can silence
the gate for free. ``gh pr view`` failing prints UNKNOWN and exits 2,
never a silent pass -- a failed read is not the same as a PR with
nothing to say. The duplicate-id check runs before the gated-path
early-return, so it fires on every invocation -- including a docs-only
PR and a bare run against ``main`` with an empty diff -- not only on a
PR that happens to touch a gated path.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "maintainer/music-dj-tools"
DEFAULT_ADR_DIR = REPO_ROOT / "docs" / "decisions"
DEFAULT_BASE = "origin/main"

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
    r"ADR:[ \t]*none[ \t]*,?[ \t]*because[ \t]+\S",
    re.IGNORECASE,
)
_ID_RE = re.compile(
    r"ADR:[ \t]*(?:ADR-)?(\d{4})\b",
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
        match = re.match(r"ADR-(\d{4})-", path.name)
        if match:
            ids.add(match.group(1))
    return ids


def duplicate_adr_ids(adr_dir: Path) -> dict[str, list[str]]:
    """ADR id -> filenames, for every id claimed by 2+ files. Empty when unique."""
    claimed: dict[str, list[str]] = {}
    if not adr_dir.is_dir():
        return {}
    for path in sorted(adr_dir.glob("ADR-*.md")):
        match = re.match(r"ADR-(\d{4})-", path.name)
        if match:
            claimed.setdefault(match.group(1), []).append(path.name)
    return {adr_id: names for adr_id, names in claimed.items() if len(names) > 1}


def ids_in_body(body: str) -> list[str]:
    return [match.group(1) for match in _ID_RE.finditer(body or "")]


def has_none_because(body: str) -> bool:
    return bool(_NONE_RE.search(body or ""))


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

    if has_none_because(body):
        return Verdict(
            0,
            f"[adr-check] OK -- ADR: none with because-reason (gated paths: {', '.join(hit)})",
        )

    return Verdict(
        1,
        "[adr-check] gated path(s) "
        + ", ".join(hit)
        + " with no ADR: <id> and no ADR: none, because <reason> on one line",
    )


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


def pr_view(pr: int, repo: str = DEFAULT_REPO) -> dict:
    proc = subprocess.run(
        ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "number,title,body"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr view failed rc={proc.returncode}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def pr_files(pr: int, repo: str = DEFAULT_REPO) -> list[str]:
    """Every path the PR changes. Paginated, because ``gh pr view --json files`` truncates."""
    proc = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{pr}/files",
            "--jq",
            ".[].filename",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh api pulls/{pr}/files failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def current_pr_number(repo_root: Path, repo: str = DEFAULT_REPO) -> int | None:
    proc = subprocess.run(
        ["gh", "pr", "view", "--repo", repo, "--json", "number"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    number = payload.get("number")
    return int(number) if number else None


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
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--body-file", default=None)
    parser.add_argument("--changed-file", action="append", default=None)
    parser.add_argument("--adr-dir", default=None)
    args = parser.parse_args(argv)

    decisions = Path(args.adr_dir) if args.adr_dir else (adr_dir or DEFAULT_ADR_DIR)

    try:
        if changed is not None:
            paths = changed
        elif args.changed_file:
            paths = list(args.changed_file)
        elif args.pr is not None:
            paths = pr_files(args.pr, args.repo)
        elif list_changed is not None:
            paths = list_changed(args.base, repo_root)
        else:
            paths = git_changed(args.base, repo_root)
    except Exception as exc:
        print(f"[adr-check] UNKNOWN: could not list changed paths ({exc})", file=sys.stderr)
        return 2

    try:
        if body is not None:
            text = body
        elif args.body_file:
            text = Path(args.body_file).read_text(encoding="utf-8")
        else:
            pr_number = args.pr
            if pr_number is None:
                pr_number = current_pr_number(repo_root, args.repo)
            if pr_number is not None:
                text = fetch(pr_number).get("body") or ""
            else:
                text = git_commit_body(args.base, repo_root)
    except Exception as exc:
        print(f"[adr-check] UNKNOWN: could not read PR/commit body ({exc})", file=sys.stderr)
        return 2

    verdict = evaluate(paths, text, decisions)
    stream = sys.stdout if verdict.code == 0 else sys.stderr
    print(verdict.message, file=stream)
    return verdict.code


if __name__ == "__main__":
    raise SystemExit(main())
