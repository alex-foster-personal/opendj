"""PERFBATCH-02/03 gate: no pipeline throughput/latency change without idle+quality.

A PR that does not touch a stems or lyrics pipeline path is measurement-only
and passes with no markers. A PR that does touch a pipeline path is classified
by the CONTENT of its hunks (PERFBATCH-07): when every added or removed line in
that file is on the narrow allowlist below, the touch is non-behavioral and
needs no markers. Anything else, including a hunk the gate cannot read, is a
pipeline mutation and must carry both of these on ONE line each, horizontal
whitespace only:

    perfbatch: pipelines-idle <non-empty-evidence>
    perfbatch-03: quality-ok reqs=<ID> signal=<path> before_after=<path>

A correctness-only filter (PERFBATCH-08) may instead carry

    perfbatch: correctness-only reqs=<ID> tests=<path>

which passes only inside the narrow scope ``[correctness_only]`` in the policy
declares: Python files under its prefixes, no deleted, renamed or mode-changed
file, a ``tests/`` path the PR itself changes, and a bounded number of changed
lines in pre-existing pipeline files. Outside that scope the marker is refused
and the idle plus quality-ok pair is still required.

Allowlist (see docs/perf/perfbatch-gates.md for the reasoning and residuals):

    Python   judged against exact ``tokenize``/``ast`` facts for the WHOLE
             file on both sides (merge base and head): blank lines; ``#``
             comment lines that are not a shebang and not a PEP 723
             metadata line; lines of a real docstring (the first statement
             of a module, class or function); widening of an
             ``except (...)`` / ``contextlib.suppress(...)`` tuple on a
             code line to a strict superset that adds no broad base;
             wrapping an existing import in
             ``try: ... except ImportError: <name> = None``. A source that
             does not tokenize on either side refuses the file.
    Markdown every line (prose is never executed).
    Other    nothing, including TypeScript, Svelte and shell: without a
             tokenizer a template literal or heredoc makes ``//`` and ``#``
             ambiguous, so any changed line is a mutation.

The gate fails closed: a pipeline path with no parseable hunks (binary, or a
name list with no diff text), or whose diff carries behavioral metadata (a
mode change, a rename or copy, a created or deleted code file), is a
mutation. ``gh pr view`` / ``gh api`` / ``gh pr diff`` / ``git diff`` failing
prints UNKNOWN and exits 2, never a silent pass. One refusal is answered
rather than reported: GitHub renders no diff over 300 files (HTTP 406), so
the gate then reads the same merge-base-to-head diff from the checkout.

The path policy (pipeline prefixes, file-level exemptions, measurement-only
prefixes) is declared in ``perfbatch_policy.toml`` next to this module and
validated at import: edit the TOML, not this file, to change it.

    python -m scripts.perf.perfbatch_gate --pr N
    python -m scripts.perf.perfbatch_gate --diff
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from scripts.perf.perfbatch_hunks import (
    FileDiff,
    SourceReader,
    classify_pipeline_paths,
    parse_unified_diff,
)

T = TypeVar("T")
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPO = "private_owner/music-dj-tools"
DEFAULT_BASE = "origin/main"
POLICY_PATH = Path(__file__).with_name("perfbatch_policy.toml")


@dataclass(frozen=True)
class Policy:
    """Path policy, declared in ``perfbatch_policy.toml`` next to this module."""

    pipeline_prefixes: tuple[str, ...]
    pipeline_exempt: tuple[str, ...]
    measurement_only_prefixes: tuple[str, ...]
    correctness_prefixes: tuple[str, ...]
    correctness_max_existing_lines: int


def _policy_list(table: dict, section: str, key: str, path: Path) -> tuple[str, ...]:
    entries = table.get(section, {}).get(key) if isinstance(table.get(section), dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: [{section}].{key} must be a non-empty list of path prefixes")
    if any(not isinstance(entry, str) or not entry.strip() for entry in entries):
        raise ValueError(f"{path}: [{section}].{key} holds a non-string or empty entry")
    return tuple(entries)


def load_policy(path: Path = POLICY_PATH) -> Policy:
    """Read and validate the TOML policy. Malformed policy refuses to start the gate."""
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: cannot read PERFBATCH policy ({exc})") from exc
    pipeline_prefixes = _policy_list(table, "pipeline", "prefixes", path)
    pipeline_exempt = _policy_list(table, "pipeline", "exempt", path)
    measurement_only_prefixes = _policy_list(table, "measurement_only", "prefixes", path)
    uncovered = [entry for entry in pipeline_exempt if not _matches_prefix(entry, pipeline_prefixes)]
    if uncovered:
        raise ValueError(f"{path}: [pipeline].exempt entries outside every prefix: {uncovered}")
    correctness_prefixes = _policy_list(table, "correctness_only", "prefixes", path)
    loose = [e for e in correctness_prefixes if not _matches_prefix(e, pipeline_prefixes)]
    if loose:
        raise ValueError(f"{path}: [correctness_only].prefixes outside every pipeline prefix: {loose}")
    return Policy(
        pipeline_prefixes=pipeline_prefixes,
        pipeline_exempt=pipeline_exempt,
        measurement_only_prefixes=measurement_only_prefixes,
        correctness_prefixes=correctness_prefixes,
        correctness_max_existing_lines=_policy_int(
            table, "correctness_only", "max_existing_lines", path
        ),
    )


def _policy_int(table: dict, section: str, key: str, path: Path) -> int:
    value = table.get(section, {}).get(key) if isinstance(table.get(section), dict) else None
    # bool is an int subclass; `true` must not read as a budget of 1.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{path}: [{section}].{key} must be a non-negative integer")
    return value


# Horizontal whitespace only. Never `\\s`: a newline as the "reason" must fail.
_IDLE = re.compile(r"perfbatch:[ \t]*pipelines-idle[ \t]+\S", re.IGNORECASE)
_QUALITY = re.compile(
    r"perfbatch-03:[ \t]*quality-ok[ \t]+reqs=\S+[ \t]+signal=\S+[ \t]+before_after=\S+",
    re.IGNORECASE,
)
_CORRECTNESS = re.compile(
    r"perfbatch:[ \t]*correctness-only[ \t]+reqs=(?P<reqs>\S+)[ \t]+tests=(?P<tests>\S+)",
    re.IGNORECASE,
)


def _matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    return any(normalized == prefix or normalized.startswith(prefix) for prefix in prefixes)


POLICY = load_policy()
PIPELINE_PREFIXES = POLICY.pipeline_prefixes
PIPELINE_EXEMPT = POLICY.pipeline_exempt
MEASUREMENT_ONLY_PREFIXES = POLICY.measurement_only_prefixes
CORRECTNESS_PREFIXES = POLICY.correctness_prefixes
CORRECTNESS_MAX_EXISTING_LINES = POLICY.correctness_max_existing_lines


def pipeline_mutation_paths(paths: list[str]) -> list[str]:
    """Pipeline-prefixed paths that are not file-level exempt (the hunk rule applies next)."""
    return [
        path
        for path in paths
        if _matches_prefix(path, PIPELINE_PREFIXES) and not _matches_prefix(path, PIPELINE_EXEMPT)
    ]


def measurement_only_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if _matches_prefix(path, MEASUREMENT_ONLY_PREFIXES)]


def idle_marker(body: str) -> bool:
    return bool(_IDLE.search(body))


def quality_marker(body: str) -> bool:
    return bool(_QUALITY.search(body))


def correctness_marker(body: str) -> tuple[str, str] | None:
    """``(reqs, tests)`` from a ``perfbatch: correctness-only`` line, or None."""
    match = _CORRECTNESS.search(body)
    return (match["reqs"], match["tests"]) if match else None


def correctness_refusal(
    mutations: list[str],
    paths: list[str],
    file_diffs: dict[str, FileDiff] | None,
    tests_path: str,
) -> str | None:
    """Why the PERFBATCH-08 correctness-only marker cannot cover these mutations, or None.

    Every reason here is something the gate can read off the diff; whether the
    filter is really correctness-only stays the author's claim, and this bounds
    how far that claim can reach.
    """
    tests = tests_path.replace("\\", "/")
    if not tests.startswith("tests/") or tests not in {p.replace("\\", "/") for p in paths}:
        return f"tests={tests_path} is not a tests/ file this PR changes"
    existing_lines = 0
    for path in mutations:
        if not _matches_prefix(path, CORRECTNESS_PREFIXES):
            return f"{path} is outside the correctness-only prefixes"
        if not path.endswith(".py"):
            return f"{path} is not a Python file"
        diff = (file_diffs or {}).get(path.replace("\\", "/"))
        if diff is None or not diff.hunks:
            return f"{path} has no readable hunks"
        if diff.binary or diff.mode_changed or diff.renamed or diff.deleted:
            return f"{path} is deleted, renamed, mode-changed or binary"
        if not diff.created:
            existing_lines += sum(
                1 for hunk in diff.hunks for tag, _ in hunk.lines if tag in ("+", "-")
            )
    if existing_lines > CORRECTNESS_MAX_EXISTING_LINES:
        return (
            f"{existing_lines} changed lines in existing pipeline files exceed "
            f"the {CORRECTNESS_MAX_EXISTING_LINES}-line correctness-only budget"
        )
    return None


# ----------------------------------------------------------------------------
# Data sources


def _run_bytes(argv: list[str], *, cwd: Path | None = None) -> bytes:
    """Raw stdout. Text mode is never used: it would translate a bare CR inside a
    diff record into a line break and hide the rest of that record (Codex P1 on
    #3804), and the Windows default code page raised on non-ASCII bytes."""
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=120, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{' '.join(argv)} failed rc={proc.returncode}: {detail}")
    return proc.stdout


def _run(argv: list[str], *, cwd: Path | None = None) -> str:
    # Undecodable bytes become U+FFFD, which is never on the allowlist, so a
    # garbled code line still fails closed. No newline translation happens.
    return _run_bytes(argv, cwd=cwd).decode("utf-8", errors="replace")


def pr_view(pr: int, repo: str = DEFAULT_REPO) -> dict:
    fields = "number,title,body,headRefOid,baseRefOid"
    out = _run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", fields])
    return json.loads(out)


def pr_diff_names(pr: int, repo: str = DEFAULT_REPO) -> list[str]:
    """Every path the PR touches, INCLUDING the old path of a rename.

    ``gh pr diff --name-only`` prints only a rename's destination, so a file
    moved out of a pipeline prefix would vanish from the candidate list and
    the full diff would never be read. The files API reports
    ``previous_filename`` for renames; ``--paginate`` walks past 30 files.
    """
    out = _run(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{pr}/files",
            "--paginate",
            "--jq",
            ".[] | .filename, (.previous_filename // empty)",
        ]
    )
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


#: What ``gh pr diff`` reports when GitHub refuses to render a diff of more than
#: 300 files. It is the one failure the checkout can answer instead.
DIFF_TOO_LARGE = "HTTP 406"


def pr_merge_base(view: dict, repo: str = DEFAULT_REPO) -> str:
    """The commit a PR's diff is taken from.

    A PR's ``baseRefOid`` is the base branch tip, not that point, so the
    compare API is asked for the merge base of the two.
    """
    compare = f"repos/{repo}/compare/{view['baseRefOid']}...{view['headRefOid']}"
    merge_base = _run(["gh", "api", compare, "--jq", ".merge_base_commit.sha"]).strip()
    if not merge_base:
        raise RuntimeError(f"compare API returned no merge base for {compare}")
    return merge_base


def git_diff_text(merge_base: str, head: str, repo_root: Path = REPO_ROOT) -> str:
    """The pipeline hunks between two commits, read from this checkout.

    Limited to the pipeline prefixes, the only paths whose hunks the gate
    judges. A candidate that falls outside the pathspec gets no hunks, which
    the classifier already treats as a mutation. A commit this checkout does
    not hold raises, so the caller prints UNKNOWN.
    """
    for sha in (merge_base, head):
        present = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        if present.returncode != 0:
            raise RuntimeError(f"commit {sha[:12]} is not in the checkout at {repo_root}")
    argv = ["git", "diff", "--no-ext-diff", "--no-color", merge_base, head, "--"]
    return _run([*argv, *PIPELINE_PREFIXES], cwd=repo_root)


def pr_diff_text(pr: int, repo: str = DEFAULT_REPO, repo_root: Path = REPO_ROOT) -> str:
    """The PR's unified diff: from GitHub, or from the checkout when GitHub refuses the size.

    The checkout holds the same two commits in CI (full-history checkout of
    the PR merge ref), so the diff of merge base to head is the same
    measurement by another transport. Any other failure still raises.
    """
    try:
        return _run(["gh", "pr", "diff", str(pr), "--repo", repo])
    except RuntimeError as exc:
        if DIFF_TOO_LARGE not in str(exc):
            raise
    view = pr_view(pr, repo)
    return git_diff_text(pr_merge_base(view, repo), view["headRefOid"], repo_root)


def _merge_base(repo_root: Path, base: str) -> str:
    return _run(["git", "merge-base", base, "HEAD"], cwd=repo_root).strip()


def local_diff_names(repo_root: Path = REPO_ROOT, base: str = DEFAULT_BASE) -> list[str]:
    """Net change of the working tree (committed, staged, unstaged) against the merge base.

    ``--no-renames`` lists a rename as its old AND new path, so a file moved
    out of a pipeline prefix stays a candidate.
    """
    argv = ["git", "diff", "--name-only", "--no-renames", _merge_base(repo_root, base)]
    out = _run(argv, cwd=repo_root)
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def local_diff_text(paths: list[str], repo_root: Path = REPO_ROOT, base: str = DEFAULT_BASE) -> str:
    return _run(["git", "diff", _merge_base(repo_root, base), "--", *paths], cwd=repo_root)


def local_sources(repo_root: Path = REPO_ROOT, base: str = DEFAULT_BASE) -> SourceReader:
    """Whole-file sources for the Python classifier: merge-base blob and working-tree file."""
    merge_base = _merge_base(repo_root, base)

    def read(path: str) -> tuple[bytes | None, bytes | None]:
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{merge_base}:{path}"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        old = (
            _run_bytes(["git", "show", f"{merge_base}:{path}"], cwd=repo_root)
            if exists.returncode == 0
            else None
        )
        target = repo_root / path
        new = target.read_bytes() if target.is_file() else None
        return old, new

    return read


def pr_sources(pr: int, repo: str = DEFAULT_REPO, view: dict | None = None) -> SourceReader:
    """Whole-file sources from GitHub: the merge-base commit and the PR head.

    The merge base comes from the compare API (a PR's ``baseRefOid`` is the
    base branch tip, not the point the diff is taken from). A 404 on the
    contents API means the file is absent on that side; any other failure
    raises so the gate prints UNKNOWN rather than guessing.
    """
    pr_json = view if view is not None else pr_view(pr, repo)
    head = pr_json["headRefOid"]
    merge_base = pr_merge_base(pr_json, repo)

    def contents(ref: str, path: str) -> bytes | None:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/{path}?ref={ref}", "--jq", ".content"],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            if "HTTP 404" in detail:
                return None
            raise RuntimeError(f"contents API failed for {path}@{ref[:9]}: {detail}")
        return base64.b64decode(proc.stdout)

    def read(path: str) -> tuple[bytes | None, bytes | None]:
        return contents(merge_base, path), contents(head, path)

    return read


# ----------------------------------------------------------------------------
# Verdict


def verdict(
    paths: list[str],
    body: str,
    file_diffs: dict[str, FileDiff] | None = None,
    sources: SourceReader | None = None,
) -> tuple[int, str]:
    candidates = pipeline_mutation_paths(paths)
    if not candidates:
        if paths and len(measurement_only_paths(paths)) == len(paths):
            return 0, "[perfbatch-gate] OK -- measurement-only (declared prefix)"
        return 0, "[perfbatch-gate] OK -- measurement-only"
    verdicts = classify_pipeline_paths(candidates, file_diffs, sources)
    mutations = [v for v in verdicts if not v.benign]
    if not mutations:
        listed = ", ".join(v.describe() for v in verdicts)
        return 0, f"[perfbatch-gate] OK -- measurement-only; allowlisted hunks only in {listed}"
    correctness = correctness_marker(body)
    refused = ""
    if correctness is not None and not (idle_marker(body) and quality_marker(body)):
        reqs, tests = correctness
        why = correctness_refusal([v.path for v in mutations], paths, file_diffs, tests)
        if why is None:
            listed = ", ".join(v.path for v in mutations)
            return (
                0,
                f"[perfbatch-gate] OK -- correctness-only filter (PERFBATCH-08) in {listed}; "
                f"reqs={reqs} tests={tests}",
            )
        refused = f" (correctness-only marker refused: {why})"
    missing: list[str] = []
    if not idle_marker(body):
        missing.append("perfbatch: pipelines-idle <evidence>")
    if not quality_marker(body):
        missing.append("perfbatch-03: quality-ok reqs= signal= before_after=")
    if not missing:
        return 0, "[perfbatch-gate] OK -- pipeline mutation cited idle+quality"
    listed = ", ".join(v.describe() for v in mutations)
    needed = "; ".join(missing)
    return (
        1,
        f"[perfbatch-gate] pipeline-mutation path(s) {listed} need {needed}{refused}",
    )


class _Unknown(RuntimeError):
    """The gate could not measure. main() prints the reason and exits 2, never a pass."""


def _measure(label: str, call: Callable[[], T]) -> T:
    try:
        return call()
    except Exception as exc:
        raise _Unknown(f"{label} ({exc})") from exc


def _gate(  # noqa: PLR0913 -- every keyword is a test seam for one data source
    args: argparse.Namespace,
    *,
    fetch,
    fetch_files,
    fetch_diff,
    fetch_sources,
    diff_names: list[str] | None,
    diff_text: str | None,
    body: str | None,
    repo_root: Path,
    base: str,
) -> tuple[int, str]:
    pr: dict = {}
    pr_body = body if body is not None else ""
    paths = list(diff_names) if diff_names is not None else None
    text = diff_text if diff_text is not None else ("" if diff_names is not None else None)

    if args.pr:
        pr = _measure(f"could not read PR #{args.pr}", lambda: fetch(args.pr, args.repo))
        pr_body = body if body is not None else (pr.get("body") or "")
        if paths is None:
            paths = _measure(
                f"could not list PR #{args.pr} files", lambda: fetch_files(args.pr, args.repo)
            )
    if paths is None:
        paths = _measure("could not read local diff", lambda: local_diff_names(repo_root, base))

    candidates = pipeline_mutation_paths(paths)
    if candidates and text is None:
        listed = ", ".join(candidates)
        text = _measure(
            f"could not read hunks for {listed}",
            lambda: (
                fetch_diff(args.pr, args.repo)
                if args.pr
                else local_diff_text(candidates, repo_root, base)
            ),
        )

    # Whole-file sources for the exact Python classifier, only when there are
    # hunks to judge; a fetch failure is UNKNOWN, never a guess.
    sources: SourceReader | None = None
    if candidates and text:
        sources = _measure(
            "could not read file sources",
            lambda: (
                fetch_sources(args.pr, args.repo, pr) if args.pr else local_sources(repo_root, base)
            ),
        )

    file_diffs = parse_unified_diff(text) if text is not None else None
    # The source readers are lazy: a contents request or blob read that fails
    # during classification is still a measurement failure, so it surfaces as
    # UNKNOWN rather than a traceback (Codex P2 on #3804).
    return _measure(
        "could not read file sources during classification",
        lambda: verdict(paths, pr_body, file_diffs, sources),
    )


def main(  # noqa: PLR0913 -- every keyword is a test seam for one data source
    argv: list[str] | None = None,
    *,
    fetch=pr_view,
    fetch_files=pr_diff_names,
    fetch_diff=pr_diff_text,
    fetch_sources=pr_sources,
    diff_names: list[str] | None = None,
    diff_text: str | None = None,
    body: str | None = None,
    repo_root: Path = REPO_ROOT,
    base: str = DEFAULT_BASE,
) -> int:
    """``diff_names`` injected without ``diff_text`` fails closed: no hunks, so mutation."""
    parser = argparse.ArgumentParser(prog="perfbatch_gate")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args(argv)
    if not args.pr and not args.diff:
        print("[perfbatch-gate] UNKNOWN: pass --pr N or --diff", file=sys.stderr)
        return 2
    try:
        code, message = _gate(
            args,
            fetch=fetch,
            fetch_files=fetch_files,
            fetch_diff=fetch_diff,
            fetch_sources=fetch_sources,
            diff_names=diff_names,
            diff_text=diff_text,
            body=body,
            repo_root=repo_root,
            base=base,
        )
    except _Unknown as exc:
        print(f"[perfbatch-gate] UNKNOWN: {exc}", file=sys.stderr)
        return 2
    stream = sys.stdout if code == 0 else sys.stderr
    print(message, file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
