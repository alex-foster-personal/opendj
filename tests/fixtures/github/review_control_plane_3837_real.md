# review_control_plane_3837_real.json

Captured Thu 1 Oct 2026 with `gh api --paginate --slurp` from open PR #3837 at its head
`b91aa9332fa593edaaf69711ce086756ab2b141c`: 428 commits, the PR whose control-plane edits
REVIEW-13 could not measure because `GET /pulls/{n}/commits` stops at 250.

- `pull`: `GET /pulls/3837`, projected to `commits`, `base.ref`, `base.sha`, `head.sha`.
- `capped_commits`: `GET /pulls/3837/commits`, all 250 records it returned, projected to `sha`
  (the only field read once the list is at the cap).
- `compare_pages`: `GET /compare/main...<head>?per_page=100`, all 5 pages in GitHub's order
  (`compare/<base.sha>...<head>` reported the same 428 commits and merge base at capture),
  each projected to `total_commits` and its commits' `sha`, `parents`, `commit.message`.

Projection, nothing invented: every record GitHub returned is present, in GitHub's order.
Measured at capture: the GraphQL `pullRequest.commits` connection reports `totalCount` 428 and
pages out the same 250 commits as the REST listing, so it is capped too.
