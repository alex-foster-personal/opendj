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
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

# ----- configuration ------------------------------------------------------------------

DEFAULT_REF = "origin/main"
DEFAULT_REPOSITORY = "private_owner/music-dj-tools"
CUTOVER_SHA = "9abb1f9c6f511442118a0f1239a12b8b861e5afa"
TRUNK_BOT_EMAIL = "85644782+trunk-io[bot]@users.noreply.github.com"
MERGIFY_BOT_EMAIL = "37929162+mergify[bot]@users.noreply.github.com"
# Raw GraphQL Actor.login for the Trunk GitHub App. `gh pr view` JSON marshals
# the same bot as `app/trunk-io`; this module talks to `gh api graphql`, so the
# prefix is absent. REST/Actions events use `trunk-io[bot]`.
TRUNK_GRAPHQL_LOGIN = "trunk-io"
GH_TIMEOUT_SECONDS = 60.0
GIT_TIMEOUT_SECONDS = 60.0
SEARCH_RESULT_CAP = 1000
SEARCH_PAGE_SIZE = 100

EXIT_OK = 0
EXIT_UNTESTED = 1
EXIT_UNKNOWN = 10

_FIELD_SEP = "\x1f"
_LOG_FORMAT = _FIELD_SEP.join(("%H", "%T", "%an", "%ae", "%cI", "%P", "%s"))
_PR_MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+) ")
_SQUASH_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")
_DRAFT_REF = re.compile(r"^trunk-(?:merge|temp)/pr-\d+/")
_SHA = re.compile(r"^[0-9a-f]{40}$")

_DRAFT_QUERY = """
query($queryString: String!, $endCursor: String) {
  search(query: $queryString, type: ISSUE, first: 100, after: $endCursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number state createdAt closedAt headRefName headRefOid
        author { login }
        headRepository { nameWithOwner }
        commits(last: 1) { nodes { commit { oid tree { oid } } } }
      }
    }
  }
}
""".strip()


class Verdict(StrEnum):
    """A row's measured merge-path status."""

    TESTED = "TESTED"
    UNTESTED = "UNTESTED"
    UNKNOWN = "UNKNOWN"


class Reason(StrEnum):
    """Stable machine-readable explanations for verdicts."""

    TRUNK_DRAFT_TREE = "trunk-draft-tree"
    DIRECT_PUSH = "direct-push"
    LEGACY_MERGIFY = "legacy-mergify"
    NO_DRAFT_TREE_MATCH = "no-draft-tree-match"
    NON_TRUNK_MERGE = "non-trunk-merge"
    ASSOCIATED_PULL = "associated-pull"
    EVIDENCE_UNREADABLE = "evidence-unreadable"


@dataclass(frozen=True)
class LocalCommit:
    """Exact local git metadata from one first-parent record."""

    sha: str
    tree_sha: str
    author_name: str
    author_email: str
    committed_at: str
    parent_shas: tuple[str, ...]
    subject: str
    pr_number: int | None


@dataclass(frozen=True)
class DraftEvidence:
    """Authenticated fields needed to tie a Trunk draft's final tree to main."""

    pr_number: int
    head_ref: str
    head_commit_sha: str
    head_tree_sha: str
    author_login: str
    head_repository: str
    created_at: str
    closed_at: str


@dataclass(frozen=True)
class EvidenceSnapshot:
    """Complete evidence already read from GitHub, ready for pure classification."""

    drafts: tuple[DraftEvidence, ...]
    associated_pulls: Mapping[str, tuple[int, ...]]
    errors: Mapping[str, str]
    draft_error: str | None = None
    repository: str = DEFAULT_REPOSITORY


@dataclass(frozen=True)
class AuditCommit:
    """One local commit plus its conservative provenance verdict."""

    sha: str
    tree_sha: str
    author_name: str
    author_email: str
    committed_at: str
    parent_shas: tuple[str, ...]
    subject: str
    pr_number: int | None
    verdict: Verdict
    reason: Reason
    evidence_detail: str | None = None
    draft: DraftEvidence | None = None


@dataclass(frozen=True)
class PageInfo:
    """GraphQL search pagination contract."""

    total: int
    has_next: bool
    end_cursor: str | None
    result_numbers: tuple[int, ...] = ()


class Unknown(RuntimeError):
    """The audit cannot measure this input safely."""


CommandRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


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
        raise Unknown(
            f"{repo} is a shallow clone, or git cannot say; fetch the full history "
            "(fetch-depth: 0)"
        )
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


def _infer_pr(subject: str) -> int | None:
    merge = _PR_MERGE_SUBJECT.match(subject)
    squash = _SQUASH_SUBJECT.search(subject)
    if merge:
        return int(merge.group(1))
    if squash:
        return int(squash.group(1))
    return None


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


def _parse_bound(flag: str, value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Unknown(f"{flag} {value!r} is not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise Unknown(f"{flag} {value!r} has no UTC offset; append Z or +00:00")
    return parsed


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
            f"{good_sha[:10]} is not on {bad_sha[:10]}'s first-parent chain; "
            "pass the main commit that was green"
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


# ----- GitHub evidence ----------------------------------------------------------------


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        argv,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    try:
        stdout = proc.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Unknown(f"{' '.join(argv[:3])} output is not UTF-8: {exc}") from exc
    stderr = proc.stderr.decode("utf-8", "backslashreplace")
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Unknown(f"{where} is not an object")
    return value


def _list(value: object, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise Unknown(f"{where} is not a list")
    return value


def _string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise Unknown(f"{where} is not a nonempty string")
    return value


def _integer(value: object, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Unknown(f"{where} is not a nonnegative integer")
    return value


def _object_id(value: object, where: str) -> str:
    object_id = _string(value, where)
    if not _SHA.fullmatch(object_id):
        raise Unknown(f"{where} is not a 40-character lowercase hex object id")
    return object_id


def _timestamp(value: object, where: str) -> str:
    timestamp = _string(value, where)
    _parse_bound(where, timestamp)
    return timestamp


def _load_json(text: str, where: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise Unknown(f"{where} returned invalid JSON: {exc}") from exc


def parse_draft_page(
    text: str,
    repository: str = DEFAULT_REPOSITORY,
) -> tuple[list[DraftEvidence], PageInfo]:
    """Strictly parse one GraphQL page and its final draft commit tree."""
    payload = _object(_load_json(text, "GitHub GraphQL"), "GraphQL payload")
    if payload.get("errors"):
        raise Unknown(f"GitHub GraphQL returned errors: {payload['errors']!r}")
    data = _object(payload.get("data"), "GraphQL data")
    search = _object(data.get("search"), "GraphQL search")
    total = _integer(search.get("issueCount"), "search.issueCount")
    page_info = _object(search.get("pageInfo"), "search.pageInfo")
    has_next = page_info.get("hasNextPage")
    if not isinstance(has_next, bool):
        raise Unknown("search.pageInfo.hasNextPage is not a boolean")
    end_cursor = page_info.get("endCursor")
    if end_cursor is not None and not isinstance(end_cursor, str):
        raise Unknown("search.pageInfo.endCursor is neither string nor null")
    drafts: list[DraftEvidence] = []
    result_numbers: list[int] = []
    for index, raw_node in enumerate(_list(search.get("nodes"), "search.nodes")):
        node = _object(raw_node, f"search.nodes[{index}]")
        number = _integer(node.get("number"), f"search.nodes[{index}].number")
        if number == 0:
            raise Unknown(f"search.nodes[{index}].number must be positive")
        result_numbers.append(number)
        head_ref = node.get("headRefName")
        if not isinstance(head_ref, str) or not head_ref.startswith(("trunk-merge/", "trunk-temp/")):
            continue
        author = _object(node.get("author"), f"search.nodes[{index}].author")
        author_login = _string(author.get("login"), f"search.nodes[{index}].author.login")
        if author_login != TRUNK_GRAPHQL_LOGIN:
            continue
        head_repository_node = _object(
            node.get("headRepository"), f"search.nodes[{index}].headRepository"
        )
        head_repository = _string(
            head_repository_node.get("nameWithOwner"),
            f"search.nodes[{index}].headRepository.nameWithOwner",
        )
        if head_repository != repository or _DRAFT_REF.match(head_ref) is None:
            continue
        if node.get("state") != "CLOSED":
            raise Unknown(f"search.nodes[{index}] is not CLOSED")
        commits = _object(node.get("commits"), f"search.nodes[{index}].commits")
        commit_nodes = _list(commits.get("nodes"), f"search.nodes[{index}].commits.nodes")
        if len(commit_nodes) != 1:
            raise Unknown(f"search.nodes[{index}] does not have exactly one last commit")
        commit = _object(
            _object(commit_nodes[0], f"search.nodes[{index}].commits.nodes[0]").get("commit"),
            f"search.nodes[{index}].commits.nodes[0].commit",
        )
        tree = _object(commit.get("tree"), f"search.nodes[{index}].commit.tree")
        head_oid = _object_id(node.get("headRefOid"), f"search.nodes[{index}].headRefOid")
        commit_oid = _object_id(commit.get("oid"), f"search.nodes[{index}].commit.oid")
        if head_oid != commit_oid:
            raise Unknown(f"search.nodes[{index}] headRefOid does not equal its last commit")
        draft = DraftEvidence(
            pr_number=number,
            head_ref=head_ref,
            head_commit_sha=head_oid,
            head_tree_sha=_object_id(
                tree.get("oid"), f"search.nodes[{index}].commit.tree.oid"
            ),
            author_login=author_login,
            head_repository=head_repository,
            created_at=_timestamp(node.get("createdAt"), f"search.nodes[{index}].createdAt"),
            closed_at=_timestamp(node.get("closedAt"), f"search.nodes[{index}].closedAt"),
        )
        drafts.append(draft)
    return drafts, PageInfo(total, has_next, end_cursor, tuple(result_numbers))


def is_authenticated_draft(draft: DraftEvidence, repository: str) -> bool:
    """Apply the repository's canonical Trunk draft predicate."""
    return (
        draft.author_login == TRUNK_GRAPHQL_LOGIN
        and draft.head_repository == repository
        and _DRAFT_REF.match(draft.head_ref) is not None
    )


def evidence_days(committed_at: Sequence[str]) -> list[str]:
    """Return each UTC day in the selected envelope, widened by one day."""
    if not committed_at:
        raise Unknown("cannot build a GitHub evidence window for zero commits")
    moments = [_parse_bound("commit timestamp", value).astimezone(UTC) for value in committed_at]
    first = min(moments).date() - timedelta(days=1)
    last = max(moments).date() + timedelta(days=1)
    days: list[str] = []
    cursor: date = first
    while cursor <= last:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


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
            drafts, page = parse_draft_page(
                self._run(argv, "GitHub draft search"), self.repository
            )
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
            raise Unknown(
                f"GitHub draft search returned {len(seen_results)} unique rows, "
                f"expected {expected_total}"
            )
        return tuple(
            draft
            for _, draft in sorted(by_pr.items())
            if is_authenticated_draft(draft, self.repository)
        )

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
                number = _integer(
                    row.get("number"), f"associated-pulls page {page_index}[{row_index}].number"
                )
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
        commit
        for commit in commits
        if commit.sha not in legacy and commit.author_email == TRUNK_BOT_EMAIL
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


# ----- pure classification ------------------------------------------------------------


def _audit_row(
    commit: LocalCommit,
    evidence: EvidenceSnapshot,
    legacy: frozenset[str],
) -> AuditCommit:
    base = asdict(commit)
    if commit.sha in legacy and commit.author_email == MERGIFY_BOT_EMAIL:
        return AuditCommit(**base, verdict=Verdict.UNKNOWN, reason=Reason.LEGACY_MERGIFY)
    if commit.author_email == TRUNK_BOT_EMAIL and commit.sha not in legacy:
        if evidence.draft_error:
            return AuditCommit(
                **base,
                verdict=Verdict.UNKNOWN,
                reason=Reason.EVIDENCE_UNREADABLE,
                evidence_detail=evidence.draft_error,
            )
        match = next(
            (
                draft
                for draft in evidence.drafts
                if is_authenticated_draft(draft, evidence.repository)
                and draft.head_tree_sha == commit.tree_sha
            ),
            None,
        )
        if match:
            return AuditCommit(
                **base,
                verdict=Verdict.TESTED,
                reason=Reason.TRUNK_DRAFT_TREE,
                evidence_detail=f"draft #{match.pr_number} {match.head_ref}",
                draft=match,
            )
        return AuditCommit(
            **base,
            verdict=Verdict.UNKNOWN,
            reason=Reason.NO_DRAFT_TREE_MATCH,
        )
    if len(commit.parent_shas) != 1:
        return AuditCommit(
            **base,
            verdict=Verdict.UNKNOWN,
            reason=Reason.NON_TRUNK_MERGE,
        )
    if commit.sha in evidence.errors:
        return AuditCommit(
            **base,
            verdict=Verdict.UNKNOWN,
            reason=Reason.EVIDENCE_UNREADABLE,
            evidence_detail=evidence.errors[commit.sha],
        )
    pulls = evidence.associated_pulls.get(commit.sha)
    if pulls is None:
        verdict = Verdict.UNKNOWN
        reason = Reason.EVIDENCE_UNREADABLE
        detail = "associated-pulls evidence was not collected"
    elif pulls:
        verdict = Verdict.UNKNOWN
        reason = Reason.ASSOCIATED_PULL
        detail = ",".join(f"#{number}" for number in pulls)
    else:
        verdict = Verdict.UNTESTED
        reason = Reason.DIRECT_PUSH
        detail = None
    return AuditCommit(**base, verdict=verdict, reason=reason, evidence_detail=detail)


def classify_commits(
    commits: Sequence[LocalCommit],
    evidence: EvidenceSnapshot,
    legacy: frozenset[str],
) -> list[AuditCommit]:
    """Classify every row from immutable local and GitHub evidence."""
    return [_audit_row(commit, evidence, legacy) for commit in commits]


def summarize(commits: Sequence[AuditCommit]) -> dict[str, object]:
    tested = sum(commit.verdict is Verdict.TESTED for commit in commits)
    untested = sum(commit.verdict is Verdict.UNTESTED for commit in commits)
    unknown = sum(commit.verdict is Verdict.UNKNOWN for commit in commits)
    coverage: float | None = None
    if not unknown:
        coverage = round(100 * tested / len(commits), 1)
    return {
        "commits": len(commits),
        "tested": tested,
        "untested": untested,
        "unknown": unknown,
        "tested_coverage_pct": coverage,
    }


def exit_code(commits: Sequence[AuditCommit]) -> int:
    if any(commit.verdict is Verdict.UNKNOWN for commit in commits):
        return EXIT_UNKNOWN
    if any(commit.verdict is Verdict.UNTESTED for commit in commits):
        return EXIT_UNTESTED
    return EXIT_OK


# ----- rendering ----------------------------------------------------------------------


def _serialize(commit: AuditCommit) -> dict[str, object]:
    payload = asdict(commit)
    payload["verdict"] = commit.verdict.value
    payload["reason"] = commit.reason.value
    return payload


def render_json(
    commits: Sequence[AuditCommit],
    *,
    repository: str,
    selection: str,
    cutover_sha: str = CUTOVER_SHA,
) -> str:
    payload = {
        "schema_version": 1,
        "repository": repository,
        "selection": selection,
        "cutover_sha": cutover_sha,
        "evidence_source": "git first-parent history + GitHub GraphQL/REST",
        "summary": summarize(commits),
        "commits": [_serialize(commit) for commit in commits],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|")


def render_markdown(commits: Sequence[AuditCommit], culprit_mode: bool) -> str:
    summary = summarize(commits)
    lines = [
        "**Trunk merge-path audit:** "
        f"{summary['tested']} TESTED, {summary['untested']} UNTESTED, "
        f"{summary['unknown']} UNKNOWN across {summary['commits']} first-parent commits.",
        "",
    ]
    if summary["unknown"]:
        lines += ["The audit is incomplete and must not be read as clean.", ""]
    elif summary["untested"]:
        if culprit_mode:
            lines += [
                "Untested suspects first: these commits did not use the proven queue path.",
                "",
            ]
    elif summary["commits"]:
        lines += ["Every selected tree has positive Trunk draft provenance.", ""]
    order = {Verdict.UNTESTED: 0, Verdict.UNKNOWN: 1, Verdict.TESTED: 2}
    ordered = sorted(enumerate(commits), key=lambda item: (order[item[1].verdict], item[0]))
    lines += [
        "| sha | committed | verdict | reason | PR | author | subject | evidence |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for _, commit in ordered:
        pr = f"#{commit.pr_number}" if commit.pr_number else "-"
        evidence = commit.evidence_detail or "-"
        lines.append(
            "| "
            + " | ".join(
                (
                    f"`{commit.sha[:10]}`",
                    commit.committed_at,
                    commit.verdict.value,
                    _cell(commit.reason.value),
                    pr,
                    _cell(commit.author_name),
                    _cell(commit.subject[:70]),
                    _cell(evidence),
                )
            )
            + " |"
        )
    return "\n".join(lines)


def render_plain(commits: Sequence[AuditCommit]) -> str:
    lines = [f"[trunk-merge-path-audit] {json.dumps(summarize(commits), sort_keys=True)}"]
    for commit in commits:
        if commit.verdict is Verdict.TESTED:
            continue
        detail = f" ({commit.evidence_detail})" if commit.evidence_detail else ""
        lines.append(
            f"{commit.verdict.value} {commit.reason.value} {commit.sha} "
            f"{commit.subject[:80]}{detail}"
        )
    return "\n".join(lines)


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
        return window_commits(args.repo, args.ref, args.since, args.until), (
            f"{args.ref} {args.since}..{args.until}"
        )
    raise Unknown("pass --range GOOD..BAD, or both --since and --until")


def _unknown_output(args: argparse.Namespace, message: str) -> str:
    if args.json:
        if args.rev_range:
            selection = args.rev_range
        elif args.since or args.until:
            selection = f"{args.ref} {args.since or '?'}..{args.until or '?'}"
        else:
            selection = args.ref
        return json.dumps(
            {
                "schema_version": 1,
                "repository": DEFAULT_REPOSITORY,
                "cutover_sha": CUTOVER_SHA,
                "selection": selection,
                "evidence_source": "git first-parent history + GitHub GraphQL/REST",
                "summary": {
                    "commits": 0,
                    "tested": 0,
                    "untested": 0,
                    "unknown": 1,
                    "tested_coverage_pct": None,
                },
                "error": message,
            },
            indent=2,
            sort_keys=True,
        )
    return f"[trunk-merge-path-audit] UNKNOWN: {message}"


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
