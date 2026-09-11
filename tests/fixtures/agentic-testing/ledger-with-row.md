# Agentic-testing fix ledger

This file tracks finding-driven fixes. Those PRs land on `at--<slug>` branches,
carry label `agentic-testing`, and append one row here. `just at-revert <pr>`
reverts that PR's merge commit on `at--revert-<pr>` and opens the revert PR.
`python -m ops.agentic_testing.check_ledger` fails when a merged PR with that
label has no row.

| utc | pr | finding | merge_sha | revert_recipe | reverted |
| --- | --- | --- | --- | --- | --- |
| 2026-09-11T12:00:00+00:00 | 9001 | 8001 | 1111111111111111111111111111111111111111 | just at-revert 9001 | |
