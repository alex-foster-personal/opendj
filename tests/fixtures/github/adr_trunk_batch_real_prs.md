# adr_trunk_batch_real_prs.json

Real captured GitHub payloads for `tests/scripts/test_adr_check_trunk_batch.py`.

## What is in it

| Key                  | What it holds                                                                  |
| -------------------- | ------------------------------------------------------------------------------ |
| `prs["<n>"]`         | `gh pr view <n> --json number,title,body,headRefName,author,isCrossRepository`, verbatim |
| `files["<n>"]`       | `gh api --paginate repos/<repo>/pulls/<n>/files --jq .[].filename`, verbatim    |
| `_fixture`           | provenance: repo, endpoint, transport, capture time UTC, description            |

PRs captured, by what each demonstrates:

- **#4301**: a Trunk Merge Queue batch authored by `app/trunk-io`, members #4225, #4194
  and #4079. It failed `adr check` on Mon 28 Sep 2026 before batches were judged by member.
- **#4306**: a Trunk bisection batch (`-bisection` head suffix) of #4259.
- **#4225, #4194, #4079**: the real members of #4301, each carrying its own declaration.
- **#1300** and **#1900**: a pair with OPPOSITE standalone verdicts. #1300 changes
  `.github/workflows/` with no ADR declaration and fails; #1900 declares
  `ADR: none, because ...` and passes. Joined, #1900's line would cover #1300's gated
  paths, which is the leak per-member evaluation closes.

## How it was captured

Through the production transport, `scripts.adr_pr_lookup.pr_view` and
`scripts.adr_pr_lookup.pr_files`, for each PR above, assembled with the `_fixture`
block and written with `json.dumps(payload, indent=1, ensure_ascii=False)` plus a
trailing newline.

## Rules

Immutable during feature work, per `AGENTS.md` -> "No mocks and locked real fixtures".
The test pins the file's sha256, so a hand edit fails loudly. If a recapture is genuinely
required, re-run the capture above, update the checksum in the same commit, and say in
the PR body why the contract moved.
