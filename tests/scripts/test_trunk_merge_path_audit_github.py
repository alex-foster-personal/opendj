"""GitHub transport tests for the Trunk merge-path provenance audit.

Requirements (DEVOPS-4215):
- [if] GitHub evidence is complete and authenticated [then] it is usable, [else stop].
- [if] paging or response structure is ambiguous [then] evidence is UNKNOWN, [else stop].
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable

import pytest

from scripts import trunk_merge_path_audit as mod

pytestmark = pytest.mark.requirement("DEVOPS-4215")


# Same Trunk batch PR as (private path) (BATCH = 4301).
_LIVE_BATCH_PR = 4301
_LIVE_GRAPHQL_LOGIN_QUERY = f"""
query {{
  repository(owner: "maintainer", name: "music-dj-tools") {{
    pullRequest(number: {_LIVE_BATCH_PR}) {{
      author {{ login }}
    }}
  }}
}}
""".strip()


def _node(number: int = 44, ref: str = "trunk-merge/pr-42/uuid") -> dict[str, object]:
    """Synthesized GraphQL-shaped fixture, not a captured payload.

    ``author.login`` is the raw GraphQL spelling ``trunk-io`` (Codex review of
    PR #9075), not the ``gh pr view`` JSON ``app/trunk-io``.
    """
    return {
        "number": number,
        "author": {"login": "trunk-io"},
        "state": "CLOSED",
        "createdAt": "2026-10-02T09:00:00Z",
        "closedAt": "2026-10-02T11:00:00Z",
        "headRefName": ref,
        "headRefOid": "c" * 40,
        "headRepository": {"nameWithOwner": mod.DEFAULT_REPOSITORY},
        "commits": {"nodes": [{"commit": {"oid": "c" * 40, "tree": {"oid": "e" * 40}}}]},
    }


def _page(
    nodes: list[dict[str, object]],
    *,
    count: int | None = None,
    more: bool = False,
    cursor: str | None = None,
) -> str:
    return json.dumps(
        {
            "data": {
                "search": {
                    "issueCount": len(nodes) if count is None else count,
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                }
            }
        }
    )


def test_graphql_page_parses_authenticated_draft() -> None:
    """[if] a complete GraphQL node is returned [then] parse exact tree evidence, [else stop]."""
    drafts, page = mod.parse_draft_page(_page([_node()]))
    assert page == mod.PageInfo(total=1, has_next=False, end_cursor=None, result_numbers=(44,))
    assert drafts[0].head_tree_sha == "e" * 40
    assert drafts[0].author_login == "trunk-io"
    assert mod.is_authenticated_draft(drafts[0], mod.DEFAULT_REPOSITORY)


def test_graphql_app_prefix_login_is_not_authenticated() -> None:
    """[if] GraphQL author.login is app/trunk-io [then] skip the node, [else stop]."""
    node = _node()
    node["author"] = {"login": "app/trunk-io"}
    drafts, page = mod.parse_draft_page(_page([node]))
    assert drafts == []
    assert page.result_numbers == (44,)


def test_live_graphql_returns_the_trunk_login() -> None:
    """[if] live GraphQL login is trunk-io [then] keep that constant, [else stop]."""
    gh = shutil.which("gh")
    if gh is None:
        pytest.skip("UNAVAILABLE: gh is not on PATH")
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["CLICOLOR_FORCE"] = "0"
    try:
        proc = subprocess.run(
            [gh, "api", "graphql", "-f", f"query={_LIVE_GRAPHQL_LOGIN_QUERY}"],
            capture_output=True,
            text=True,
            timeout=mod.GH_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"UNAVAILABLE: live gh api graphql could not reach GitHub ({exc})")
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = proc.stderr.strip() or proc.stdout.strip() or "empty response"
        pytest.skip(f"UNAVAILABLE: live gh api graphql could not reach GitHub ({detail})")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.skip(f"UNAVAILABLE: live gh api graphql could not reach GitHub ({exc})")
    errors = payload.get("errors")
    if errors and not payload.get("data"):
        pytest.skip(f"UNAVAILABLE: live gh api graphql could not reach GitHub ({errors})")
    login = payload["data"]["repository"]["pullRequest"]["author"]["login"]
    assert login == "trunk-io"
    assert login == mod.TRUNK_GRAPHQL_LOGIN


def test_ordinary_merged_pr_does_not_poison_trunk_draft() -> None:
    """[if] an ordinary merged PR shares a page [then] parse the Trunk draft, [else stop]."""
    ordinary = {
        "number": 7,
        "author": None,
        "state": "MERGED",
        "createdAt": "2026-10-02T08:00:00Z",
        "closedAt": "2026-10-02T10:00:00Z",
        "headRefName": "feature/ordinary",
        "headRefOid": None,
        "headRepository": None,
        "commits": {"nodes": []},
    }
    drafts, page = mod.parse_draft_page(_page([ordinary, _node()], count=2))
    assert [draft.pr_number for draft in drafts] == [44]
    assert page.result_numbers == (7, 44)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda node: node.pop("closedAt"),
        lambda node: node.update(author=None),
        lambda node: node.update(headRepository=None),
        lambda node: node.update(commits={"nodes": []}),
        lambda node: node.update(headRefOid="not-a-sha"),
        lambda node: node.update(closedAt="not-a-time"),
        lambda node: node.update(number=0),
    ],
)
def test_graphql_required_fields_fail_closed(
    mutate: Callable[[dict[str, object]], object],
) -> None:
    """[if] a required GraphQL field is null or absent [then] parsing fails, [else stop]."""
    node = _node()
    mutate(node)
    with pytest.raises(mod.Unknown):
        mod.parse_draft_page(_page([node]))


def test_reader_paginates_and_deduplicates() -> None:
    """[if] pages repeat a PR [then] the reader reconciles and deduplicates it, [else stop]."""
    first = [_node(number) for number in range(1, 101)]
    outputs = iter(
        [
            _page(first, count=101, more=True, cursor="next"),
            _page([_node(44), _node(101)], count=101),
        ]
    )

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        assert timeout == mod.GH_TIMEOUT_SECONDS
        return subprocess.CompletedProcess(argv, 0, next(outputs), "")

    reader = mod.GitHubEvidenceReader(mod.DEFAULT_REPOSITORY, runner=runner)
    drafts = reader.fetch_drafts_for_day("2026-10-02")
    assert len(drafts) == 101
    assert drafts[0].pr_number == 1 and drafts[-1].pr_number == 101


@pytest.mark.parametrize(
    "outputs",
    [
        [
            _page(
                [_node(number) for number in range(1, 101)],
                count=101,
                more=True,
                cursor=None,
            )
        ],
        [
            _page([_node(number) for number in range(1, 101)], count=200, more=True, cursor="same"),
            _page([_node(number) for number in range(101, 201)], count=200, more=True, cursor="same"),
        ],
        [
            _page([_node(number) for number in range(1, 101)], count=101, more=True, cursor="next"),
            _page([_node(101)], count=102),
        ],
        [_page([], count=1001)],
        [_page([_node()], count=101, more=True, cursor="next")],
    ],
)
def test_ambiguous_or_truncated_paging_is_unknown(outputs: list[str]) -> None:
    """[if] GraphQL paging is incomplete [then] the reader raises UNKNOWN, [else stop]."""
    pages = iter(outputs)

    def runner(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, next(pages), "")

    with pytest.raises(mod.Unknown):
        mod.GitHubEvidenceReader(mod.DEFAULT_REPOSITORY, runner=runner).fetch_drafts_for_day(
            "2026-10-02"
        )


def test_associated_pull_pagination_distinguishes_empty_and_nonempty() -> None:
    """[if] REST paging completes [then] empty and nonempty evidence differ, [else stop]."""
    first_page = [{"number": number} for number in range(1, 101)]
    outputs = iter(["[[]]", json.dumps([first_page, [{"number": 101}]])])

    def runner(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        assert "--paginate" in argv and "--slurp" in argv
        return subprocess.CompletedProcess(argv, 0, next(outputs), "")

    reader = mod.GitHubEvidenceReader(mod.DEFAULT_REPOSITORY, runner=runner)
    assert reader.fetch_associated_pulls("a" * 40) == ()
    assert reader.fetch_associated_pulls("b" * 40) == tuple(range(1, 102))


def test_bare_empty_associated_pull_slurp_is_unknown() -> None:
    """[if] REST slurp has no outer pages [then] evidence is UNKNOWN, [else stop]."""

    def runner(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    reader = mod.GitHubEvidenceReader(mod.DEFAULT_REPOSITORY, runner=runner)
    with pytest.raises(mod.Unknown, match="no pages"):
        reader.fetch_associated_pulls("a" * 40)


def test_differing_duplicate_draft_is_unknown() -> None:
    """[if] a draft changes across pages [then] the evidence is UNKNOWN, [else stop]."""
    first = [_node(number) for number in range(1, 101)]
    changed = _node(44)
    changed["headRefOid"] = "a" * 40
    changed["commits"] = {"nodes": [{"commit": {"oid": "a" * 40, "tree": {"oid": "b" * 40}}}]}
    outputs = iter(
        [
            _page(first, count=100, more=True, cursor="next"),
            _page([changed], count=100),
        ]
    )

    def runner(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, next(outputs), "")

    with pytest.raises(mod.Unknown, match="changed during pagination"):
        mod.GitHubEvidenceReader(runner=runner).fetch_drafts_for_day("2026-10-02")


@pytest.mark.parametrize("mode", ["nonzero", "json", "timeout"])
def test_transport_failures_are_unknown(mode: str) -> None:
    """[if] gh fails or returns bad JSON [then] evidence is UNKNOWN, [else stop]."""
    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if mode == "nonzero":
            return subprocess.CompletedProcess(argv, 1, "", "denied")
        return subprocess.CompletedProcess(argv, 0, "not-json", "")

    reader = mod.GitHubEvidenceReader(mod.DEFAULT_REPOSITORY, runner=runner)
    with pytest.raises(mod.Unknown):
        reader.fetch_associated_pulls("a" * 40)


def test_date_envelope_is_split_into_individual_utc_days() -> None:
    """[if] a commit window spans days [then] query each widened UTC day, [else stop]."""
    days = mod.evidence_days(["2026-10-02T23:30:00+00:00", "2026-10-04T00:30:00+00:00"])
    assert days == ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"]
