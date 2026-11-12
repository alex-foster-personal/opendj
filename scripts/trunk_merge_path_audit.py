#!/usr/bin/env python3
"""Audit which first-parent main commits have positive Trunk queue provenance.

The audit is deliberately fail closed. A commit is ``TESTED`` only when its exact tree is
also the final tree of an authenticated same-repository Trunk draft and the main commit was
authored with Trunk's stable noreply email. A direct push is ``UNTESTED`` only after GitHub
confirms that the one-parent commit belongs to no pull request. Everything else is ``UNKNOWN``.

MINI-PRD (DEVOPS-4215)
    R1 Exact Trunk identity plus authenticated tree equality is TESTED .......... done
       [if] either half of the proof is absent [then] UNKNOWN, never TESTED [else stop]
    R2 A positively identified direct push is UNTESTED ........................ done
       [if] commit-to-pulls evidence is absent or nonempty [then] UNKNOWN [else stop]
    R3 Legacy and unreadable evidence fail closed .............................. done
       [if] any selected row is UNKNOWN [then] exit 10 and null coverage [else stop]

Read-only usage::

    python -m scripts.trunk_merge_path_audit --since 2026-10-01T00:00Z \
        --until 2026-10-02T00:00Z --json
    python -m scripts.trunk_merge_path_audit --range GOOD..BAD --markdown
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from scripts.trunk_merge_path_audit_evidence import (  # noqa: F401
    _DRAFT_QUERY,
    _DRAFT_REF,
    _FIELD_SEP,
    _LOG_FORMAT,
    _PR_MERGE_SUBJECT,
    _SHA,
    _SQUASH_SUBJECT,
    CUTOVER_SHA,
    DEFAULT_REPOSITORY,
    EXIT_OK,
    EXIT_UNKNOWN,
    EXIT_UNTESTED,
    GH_TIMEOUT_SECONDS,
    GIT_TIMEOUT_SECONDS,
    MERGIFY_BOT_EMAIL,
    SEARCH_PAGE_SIZE,
    SEARCH_RESULT_CAP,
    TRUNK_BOT_EMAIL,
    TRUNK_GRAPHQL_LOGIN,
    AuditCommit,
    CommandRunner,
    DraftEvidence,
    EvidenceSnapshot,
    LocalCommit,
    PageInfo,
    Reason,
    Unknown,
    Verdict,
    _audit_row,
    _cell,
    _default_runner,
    _infer_pr,
    _integer,
    _list,
    _load_json,
    _object,
    _object_id,
    _parse_bound,
    _serialize,
    _string,
    _timestamp,
    classify_commits,
    evidence_days,
    exit_code,
    is_authenticated_draft,
    parse_draft_page,
    render_json,
    render_markdown,
    render_plain,
    summarize,
)
from scripts.trunk_merge_path_audit_report import (
    _unknown_output,
)

# ----- configuration ------------------------------------------------------------------

DEFAULT_REF = "origin/main"


# ----- local history ------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git without replace objects and decode stdout as strict UTF-8."""
    git = shutil.which("git")
    if git is None:
        raise Unknown("git is not on PATH")
    argv = [git, "--no-replace-objects", "-C", str(repo), *args]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise Unknown(f"git {args[0]} timed out after {GIT_TIMEOUT_SECONDS:g}s") from exc
    except OSError as exc:
        raise Unknown(f"git {args[0]} could not start: {exc}") from exc
    try:
        stdout = proc.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Unknown(f"git {args[0]} output is not UTF-8: {exc}") from exc
    stderr = proc.stderr.decode("utf-8", "backslashreplace")
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def require_complete_history(repo: Path) -> None:
    """Reject local history whose parent graph can omit or rewrite commits."""
    shallow = _git(repo, "rev-parse", "--is-shallow-repository")
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        raise Unknown(f"{repo} is a shallow clone, or git cannot say; fetch the full history (fetch-depth: 0)")
    grafts = _git(repo, "rev-parse", "--git-path", "info/grafts")
    if grafts.returncode != 0 or not grafts.stdout.strip():
        raise Unknown(f"git rev-parse --git-path info/grafts failed: {grafts.stderr.strip()}")
    graft_path = Path(grafts.stdout.strip())
    if not graft_path.is_absolute():
        graft_path = repo / graft_path
    if graft_path.exists():
        raise Unknown(f"{grafts.stdout.strip()} exists: grafts rewrite the history being audited")


def _resolve(repo: Path, ref: str) -> str:
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise Unknown(f"ref {ref!r} does not resolve to a commit in {repo}")
    return proc.stdout.strip()


def first_parent_commits(repo: Path, rev_args: list[str]) -> list[LocalCommit]:
    """Read exact NUL-framed first-parent records from git."""
    require_complete_history(repo)
    proc = _git(repo, "log", "-z", "--first-parent", f"--format={_LOG_FORMAT}", *rev_args)
    if proc.returncode != 0:
        raise Unknown(f"git log {' '.join(rev_args)} failed: {proc.stderr.strip()}")
    records = proc.stdout.split("\0")
    if not records or records.pop() != "":
        raise Unknown(f"git log {' '.join(rev_args)} output does not end with a NUL")
    commits: list[LocalCommit] = []
    for record in records:
        fields = record.split(_FIELD_SEP)
        if len(fields) != 7:
            raise Unknown(f"unparseable git log record: {record!r}")
        sha, tree_sha, author_name, author_email, committed_at, parents, subject = fields
        if not _SHA.fullmatch(sha) or not _SHA.fullmatch(tree_sha):
            raise Unknown(f"unparseable git object ids in record: {record!r}")
        parent_shas = tuple(parents.split())
        if any(not _SHA.fullmatch(parent) for parent in parent_shas):
            raise Unknown(f"unparseable parent ids in record: {record!r}")
        commits.append(
            LocalCommit(
                sha,
                tree_sha,
                author_name,
                author_email,
                committed_at,
                parent_shas,
                subject,
                _infer_pr(subject),
            )
        )
    return commits


def window_commits(repo: Path, ref: str, since: str, until: str) -> list[LocalCommit]:
    since_at = _parse_bound("--since", since)
    until_at = _parse_bound("--until", until)
    if since_at >= until_at:
        raise Unknown(f"--since {since!r} is not before --until {until!r}")
    _resolve(repo, ref)
    commits = first_parent_commits(
        repo,
        [f"--since-as-filter={since_at.isoformat()}", f"--until={until_at.isoformat()}", ref],
    )
    if not commits:
        raise Unknown(f"zero first-parent commits on {ref} between {since} and {until}")
    return commits


def range_commits(repo: Path, good: str, bad: str) -> list[LocalCommit]:
    good_sha = _resolve(repo, good)
    bad_sha = _resolve(repo, bad)
    ancestor = _git(repo, "merge-base", "--is-ancestor", good_sha, bad_sha)
    if ancestor.returncode == 1:
        raise Unknown(f"{good_sha[:10]} is not an ancestor of {bad_sha[:10]}")
    if ancestor.returncode != 0:
        raise Unknown(f"git merge-base failed: {ancestor.stderr.strip()}")
    commits = first_parent_commits(repo, [f"{good_sha}..{bad_sha}"])
    if not commits:
        raise Unknown(f"empty range {good_sha[:10]}..{bad_sha[:10]}: GOOD equals BAD")
    oldest_parent = commits[-1].parent_shas[0] if commits[-1].parent_shas else None
    if oldest_parent != good_sha:
        raise Unknown(
            f"{good_sha[:10]} is not on {bad_sha[:10]}'s first-parent chain; pass the main commit that was green"
        )
    return commits


def legacy_shas(repo: Path, commits: Sequence[LocalCommit], cutover_sha: str) -> frozenset[str]:
    """Partition selected commits around the cutover, rejecting unrelated histories."""
    cutover = _resolve(repo, cutover_sha)
    legacy: set[str] = set()
    for commit in commits:
        after = _git(repo, "merge-base", "--is-ancestor", cutover, commit.sha)
        if after.returncode == 0:
            continue
        if after.returncode not in {0, 1}:
            raise Unknown(f"cannot compare {commit.sha[:10]} to cutover: {after.stderr.strip()}")
        before = _git(repo, "merge-base", "--is-ancestor", commit.sha, cutover)
        if before.returncode == 0:
            legacy.add(commit.sha)
        elif before.returncode == 1:
            raise Unknown(f"{commit.sha[:10]} is unrelated to cutover {cutover[:10]}")
        else:
            raise Unknown(f"cannot compare {commit.sha[:10]} to cutover: {before.stderr.strip()}")
    return frozenset(legacy)


class GitHubEvidenceReader:
    """One strict, bounded, read-only GitHub evidence boundary."""

    def __init__(
        self,
        repository: str = DEFAULT_REPOSITORY,
        *,
        runner: CommandRunner = _default_runner,
    ) -> None:
        self.repository = repository
        self._runner = runner
        gh = shutil.which("gh")
        if runner is _default_runner and gh is None:
            raise Unknown("gh is not on PATH")
        self._gh = gh or "gh"

    def _run(self, argv: list[str], label: str) -> str:
        try:
            proc = self._runner(argv, GH_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise Unknown(f"{label} timed out after {GH_TIMEOUT_SECONDS:g}s") from exc
        except OSError as exc:
            raise Unknown(f"{label} could not start: {exc}") from exc
        if proc.returncode != 0:
            raise Unknown(f"{label} failed rc={proc.returncode}: {proc.stderr.strip()}")
        return proc.stdout

    def _run_json(self, argv: list[str], label: str) -> Any:
        return _load_json(self._run(argv, label), label)

    def fetch_drafts_for_day(self, day: str) -> tuple[DraftEvidence, ...]:
        """Read every closed PR created on one UTC day, then authenticate draft refs."""
        query_string = f"repo:{self.repository} is:pr is:closed closed:{day}..{day}"
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_results: set[int] = set()
        by_pr: dict[int, DraftEvidence] = {}
        expected_total: int | None = None
        while True:
            argv = [
                self._gh,
                "api",
                "graphql",
                "-f",
                f"query={_DRAFT_QUERY}",
                "-F",
                f"queryString={query_string}",
            ]
            if cursor is not None:
                argv += ["-F", f"endCursor={cursor}"]
            drafts, page = parse_draft_page(self._run(argv, "GitHub draft search"), self.repository)
            if page.total > SEARCH_RESULT_CAP:
                raise Unknown(f"GitHub draft search has {page.total} results, above the API cap")
            if expected_total is None:
                expected_total = page.total
            elif expected_total != page.total:
                raise Unknown("GitHub draft search result count changed during pagination")
            if page.has_next and len(page.result_numbers) < SEARCH_PAGE_SIZE:
                raise Unknown("GitHub draft search returned a short non-final page")
            seen_results.update(page.result_numbers)
            for draft in drafts:
                prior = by_pr.get(draft.pr_number)
                if prior is not None and prior != draft:
                    raise Unknown(f"GitHub draft PR #{draft.pr_number} changed during pagination")
                by_pr[draft.pr_number] = draft
            if not page.has_next:
                break
            if not page.end_cursor or page.end_cursor in seen_cursors:
                raise Unknown("GitHub draft search cursor is missing or repeated")
            seen_cursors.add(page.end_cursor)
            cursor = page.end_cursor
        if expected_total is None or len(seen_results) != expected_total:
            raise Unknown(f"GitHub draft search returned {len(seen_results)} unique rows, expected {expected_total}")
        return tuple(draft for _, draft in sorted(by_pr.items()) if is_authenticated_draft(draft, self.repository))

    def fetch_drafts(self, committed_at: Sequence[str]) -> tuple[DraftEvidence, ...]:
        by_pr: dict[int, DraftEvidence] = {}
        for day in evidence_days(committed_at):
            for draft in self.fetch_drafts_for_day(day):
                prior = by_pr.get(draft.pr_number)
                if prior is not None and prior != draft:
                    message = f"GitHub draft PR #{draft.pr_number} differs across date windows"
                    raise Unknown(message)
                by_pr[draft.pr_number] = draft
        return tuple(draft for _, draft in sorted(by_pr.items()))

    def fetch_associated_pulls(self, sha: str) -> tuple[int, ...]:
        """Read all pull requests associated with a commit through REST pagination."""
        endpoint = f"repos/{self.repository}/commits/{sha}/pulls?per_page=100"
        payload = self._run_json(
            [
                self._gh,
                "api",
                "--paginate",
                "--slurp",
                "-H",
                "Accept: application/vnd.github+json",
                endpoint,
            ],
            f"GitHub associated-pulls lookup for {sha[:10]}",
        )
        pages = _list(payload, "associated-pulls response")
        if not pages:
            raise Unknown("associated-pulls response has no pages")
        if all(isinstance(item, dict) for item in pages):
            pages = [pages]
        numbers: set[int] = set()
        for page_index, raw_page in enumerate(pages):
            page = _list(raw_page, f"associated-pulls page {page_index}")
            if len(page) > 100:
                raise Unknown(f"associated-pulls page {page_index} exceeds requested size")
            if page_index < len(pages) - 1 and len(page) < 100:
                raise Unknown(f"associated-pulls page {page_index} is a short non-final page")
            for row_index, raw_row in enumerate(page):
                row = _object(raw_row, f"associated-pulls page {page_index}[{row_index}]")
                number = _integer(row.get("number"), f"associated-pulls page {page_index}[{row_index}].number")
                if number == 0:
                    raise Unknown("associated pull number must be positive")
                numbers.add(number)
        return tuple(sorted(numbers))


def collect_evidence(
    commits: Sequence[LocalCommit],
    legacy: frozenset[str],
    reader: GitHubEvidenceReader,
) -> EvidenceSnapshot:
    """Read evidence while preserving a named error for every affected row."""
    trunk_candidates = [
        commit for commit in commits if commit.sha not in legacy and commit.author_email == TRUNK_BOT_EMAIL
    ]
    drafts: tuple[DraftEvidence, ...] = ()
    draft_error: str | None = None
    if trunk_candidates:
        try:
            drafts = reader.fetch_drafts([commit.committed_at for commit in trunk_candidates])
        except Unknown as exc:
            draft_error = str(exc)
    pulls: dict[str, tuple[int, ...]] = {}
    errors: dict[str, str] = {}
    for commit in commits:
        if len(commit.parent_shas) != 1 or commit.author_email == TRUNK_BOT_EMAIL:
            continue
        if commit.sha in legacy and commit.author_email == MERGIFY_BOT_EMAIL:
            continue
        try:
            pulls[commit.sha] = reader.fetch_associated_pulls(commit.sha)
        except Unknown as exc:
            errors[commit.sha] = str(exc)
    return EvidenceSnapshot(drafts, pulls, errors, draft_error, reader.repository)


# ----- CLI ----------------------------------------------------------------------------


def _selection(args: argparse.Namespace) -> tuple[list[LocalCommit], str]:
    if args.rev_range:
        if args.since or args.until:
            raise Unknown("--range cannot be combined with --since or --until")
        if args.rev_range.count("..") != 1 or "..." in args.rev_range:
            raise Unknown(f"--range must be GOOD..BAD, got {args.rev_range!r}")
        good, bad = args.rev_range.split("..", 1)
        if not good or not bad:
            raise Unknown(f"--range must be GOOD..BAD, got {args.rev_range!r}")
        return range_commits(args.repo, good, bad), args.rev_range
    if args.since and args.until:
        return window_commits(args.repo, args.ref, args.since, args.until), (f"{args.ref} {args.since}..{args.until}")
    raise Unknown("pass --range GOOD..BAD, or both --since and --until")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--range", dest="rev_range", help="GOOD..BAD culprit range")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)
    try:
        commits, selection = _selection(args)
        legacy = legacy_shas(args.repo, commits, CUTOVER_SHA)
        reader = GitHubEvidenceReader(DEFAULT_REPOSITORY)
        evidence = collect_evidence(commits, legacy, reader)
        audited = classify_commits(commits, evidence, legacy)
    except Unknown as exc:
        print(_unknown_output(args, str(exc)))
        return EXIT_UNKNOWN
    if args.json:
        print(
            render_json(
                audited,
                repository=DEFAULT_REPOSITORY,
                selection=selection,
                cutover_sha=CUTOVER_SHA,
            )
        )
    elif args.markdown:
        print(render_markdown(audited, culprit_mode=bool(args.rev_range)))
    else:
        print(render_plain(audited))
    return exit_code(audited)


if __name__ == "__main__":
    sys.exit(main())
