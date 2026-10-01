"""Every review-artifact endpoint must be paginated, not just its first page.

Split into its own module for the same reason as test_review_coverage_head_tie.py:
scripts/review_coverage.py already sits close to the 600-line file-size ratchet.

issue #1016 P2 BLOCKING, thread r3927558605 (PR #1053, Thu 3 Sep 2026): a plain
`gh api <endpoint>` call only fetches page 1. On a long-lived PR whose
current-head evidence has scrolled past page 1, coverage would read a false
MISS despite a real review sitting on a later page -- the opposite failure
direction from the head-tie findings, but the same root cause: trusting an
incomplete read as a complete one.

issue #1016 P1 BLOCKING, thread r3927877691 (same PR, later round): this
module's first draft replaced `_gh` with a lambda, which this repo's
AGENTS.md ("No mocks and locked real fixtures") explicitly bars -- a green
run never touched the production subprocess path. `_flatten_pages` is now a
pure function (scripts/review_gh.py) tested below on literal data, no `_gh`
involved at all, and the `--paginate --slurp` wiring is instead proven by a
real CLI integration test against a live PR through the unmodified
production path.

issue #1016 P1 BLOCKING, thread r3929608593 (same PR, later round): that live
test skipped only on a missing `gh` binary, not on a present-but-unauthenticated
one -- exactly this repo's own `pytest fast lane + reqs-check` CI step, which
runs `gh` without `GH_TOKEN`. `_gh_unavailable_reason` now also skips on a
failing `gh auth status`.

Thu 1 Oct 2026: authentication was not enough either. PR CI now exports a token, so the
test ran live on every PR and spent the shared runner token's quota; when that ran out
(`API rate limit exceeded for user ID 166056029`, run 36823458339) it turned PR #4678
red on code the PR never touched. It now runs only under `MDT_LIVE_GITHUB=1`
(tests/support/live_github.py), like every other live-API test here.
"""

from __future__ import annotations

from scripts.review_coverage import REPO
from scripts.review_gh import _flatten_pages, _paginated_json_list
from tests.support.live_github import live_github

# ----- _flatten_pages: pure, no `_gh` involved -----------------------------


def test_a_single_page_is_flattened_to_a_plain_list() -> None:
    """`gh api --paginate --slurp` wraps even a single page in an outer
    array, so a one-page response is `[[...]]`, not `[...]` -- the flatten
    step must still hand callers a plain list."""
    page_one = [{"id": 1}, {"id": 2}]
    assert _flatten_pages([page_one]) == page_one


def test_multiple_pages_are_flattened_into_one_list() -> None:
    """THE CASE THE P2 NAMED: evidence on page 2 must survive the flatten,
    not be dropped the way an unpaginated call would drop it."""
    page_one = [{"id": 1}, {"id": 2}]
    page_two = [{"id": 3}]
    assert _flatten_pages([page_one, page_two]) == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_an_empty_result_set_flattens_to_an_empty_list() -> None:
    assert _flatten_pages([[]]) == []


def test_no_pages_at_all_flattens_to_an_empty_list() -> None:
    assert _flatten_pages([]) == []


# ----- _paginated_json_list: real CLI integration test, no mock ------------


@live_github
def test_paginated_json_list_flattens_a_real_endpoint_through_the_real_gh_cli(gh_authenticated: None) -> None:
    """issue #1016 P1 BLOCKING, thread r3927877691: proves the live `gh api
    <endpoint> --paginate --slurp` wiring through the actual `_gh` subprocess
    call against a real, permanently-queryable endpoint (this PR's own review
    list stays queryable on GitHub regardless of merge state), rather than
    standing in for `_gh` with fabricated output. If `--slurp` were dropped,
    a real multi-item response here is still a flat `[...]` from `gh`, which
    `_flatten_pages` would then iterate a level too deep and crash on a
    dict's `.get` never being called -- so a wiring regression here fails
    loudly, not silently.
    """
    result = _paginated_json_list(f"repos/{REPO}/pulls/1053/reviews")
    assert isinstance(result, list)
    assert len(result) > 0
    assert all(isinstance(item, dict) for item in result)
    assert all("user" in item for item in result)
