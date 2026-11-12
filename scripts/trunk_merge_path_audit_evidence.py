"""Part of `trunk_merge_path_audit.py`, split out with no behavior change: evidence."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

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


def _infer_pr(subject: str) -> int | None:
    merge = _PR_MERGE_SUBJECT.match(subject)
    squash = _SQUASH_SUBJECT.search(subject)
    if merge:
        return int(merge.group(1))
    if squash:
        return int(squash.group(1))
    return None


def _parse_bound(flag: str, value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Unknown(f"{flag} {value!r} is not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise Unknown(f"{flag} {value!r} has no UTC offset; append Z or +00:00")
    return parsed


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
        head_repository_node = _object(node.get("headRepository"), f"search.nodes[{index}].headRepository")
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
            head_tree_sha=_object_id(tree.get("oid"), f"search.nodes[{index}].commit.tree.oid"),
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
                if is_authenticated_draft(draft, evidence.repository) and draft.head_tree_sha == commit.tree_sha
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
        lines.append(f"{commit.verdict.value} {commit.reason.value} {commit.sha} {commit.subject[:80]}{detail}")
    return "\n".join(lines)
