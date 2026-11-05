# Bundle budgets: base limits plus per-PR charges

`scripts/bundle-budget.mjs` (run by `scripts/check-bundle-size.sh` after `pnpm build`) enforces one
gzip budget per surface. The numbers live here, not in the script
(ADR-NEW-bundle-budget-charges):

```text
budgets/<surface>/base.json            base limit, attribution rule, last measurement
budgets/<surface>/charges/<pr>.json    one PR's measured growth of that surface
budgets/<surface>/HISTORY.md           write-once record of the raises before charges
```

The gate enforces `limit = base.limit + sum(charge.bytes)` and prints that arithmetic for every
surface. Since Wed 4 Nov 2026 (ADR-NEW-bundle-budget-pr-delta) that ceiling is read in two ways:

- A **pull request** is charged by its own delta: each surface's bytes minus the same surface in
  main's measurement at the merge base, against the PR's own allowance, the sum of its own charge
  files (default 0), plus a 128 B gzip build-noise floor per surface (`PR_DELTA_NOISE_BYTES`; two builds of the same source differ by 1 to 21 B). The absolute ceiling is only a warning on a PR, so main being over never reds
  a PR that did not grow anything. Main's measurement is published by every push to main
  (`bundle-measure-<sha>`); when it is not there yet the job builds the merge base and says so.
- A **push to main** measures, publishes and warns; the **nightly** schedule keeps the absolute
  ceiling and opens the issue "Bundle budget ceiling crossed on main (nightly ratchet)" on a
  crossing. Main's absolute size is a nightly ratchet; the repair is a rebaseline (below). Before Tue 3 Nov 2026 every PR that grew a surface rewrote one shared `limit:` number, so
that file sat in 60 of 293 conflicting re-merges of main in a week. A PR now only ADDS a file.

## My PR grows a surface

1. Measure the surface on main and on main plus your PR, with the same build host:
   `pnpm build && bash scripts/check-bundle-size.sh --json` (read `totals.<surface>.bytes`).
2. Add `budgets/<surface>/charges/<pr>.json`. Before the PR number exists, name it after your
   branch (`af--my-branch.json`) and rename it once the PR is open. The gate reads both the PR
   number and the branch name as this PR's own charges (`--own`); a charge named for another PR
   is not your allowance.

```json
{
  "bytes": 2048,
  "measured_main": 299240,
  "measured_with_pr": 300897,
  "sha": "7837e09617",
  "date": "Tue 3 Nov 2026",
  "reason": "what grew and why it has to be on this surface",
  "payback": "how the bytes come back later, or 'none planned' with the reason"
}
```

Rules, all checked by the gate (a bad file fails the run, it is never skipped):

- Exactly these seven keys. `sha` is the commit you measured (7 to 40 hex chars).
- `date` reads `Ddd D Mon YYYY`, and the weekday must match the date.
- `measured_with_pr` is above `measured_main`; a PR that does not grow the surface needs no charge.
- `bytes` is at most the growth ceiled to KiB plus 1 KiB, the same headroom rule as a rebaseline.
  Size it to your own growth only. Do not cover bytes main already owes: if main alone is over,
  that is a rebaseline (below), not your charge.
- Never edit a `base.json` in a feature PR, and never edit another PR's charge.

## A new lazy chunk

A new lazily loaded feature that should not ride `other-lazy` gets its own surface: a new directory
with a `base.json` and no code change. Attribute it by content markers, strings only its chunks
carry (`"attribution": {"markers": ["..."]}`); every marker must be found in a lazy chunk or the
gate fails. Give it an `order` between `performance` (20) and `other-lazy` (90): a file is charged
to the first surface that reaches it. Its first `limit` follows the rebaseline rule.

## Rebaseline (main only)

Folds the charges into the base, like towncrier's build step. On one commit of main, measure every
surface, then for each: `limit = measured, ceiled to KiB, plus 1 KiB`, `measured` and
`measured_on` and `baseline_sha` from that build, and delete the folded charge files in the same
commit. Update `EXPECTED_BASE` in `tests/unit/bundle-budget.test.mjs`, which pins every base. A
rebaseline is the only edit a base ever gets.

Proposal: for a preventive rebaseline of a surface still under its ceiling, use `limit = CI measured + 2%, ceiled to KiB`, because plus 1 KiB is below build noise on surfaces of several hundred KB; keep the rule above for the red-fix case.
