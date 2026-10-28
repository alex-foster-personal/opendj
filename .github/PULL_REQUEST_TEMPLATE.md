## What changed
Briefly describe the changes in this PR.

## Why
Why are these changes necessary?

## Requirement ID(s)
Which requirement ID(s) from `REQUIREMENTS.md` are touched? (e.g., SYNC-01)

## ADR
ADR: <ADR-NNNN, ADR: NEW, or 'none, because ...'>
See `(private path)`. Required when the diff touches `apps/cloud/`,
`apps/engine_core/`, `apps/webui/server/state*`, `.planning/REQUIREMENTS.md`,
or a top-level dependency file.

## Tests added
- [ ] List tests added or link to test files
- [ ] No tests required because: ...

## CI tests
<!-- Minimal required set ((private path)). Each field is read only when it
starts its own line, so these examples are inert until you write your own:
  e.g. CI-Tests: (private path) -- reads the seed ledger this PR changes
  e.g. CI-Wide-Reason: this PR changes conftest.py, which configures every test
CI-Tests adds a module the selector cannot see. CI-Wide-Reason is required when CI runs
the full suite or more than half the lane. -->

## Live-DB risk
- [ ] None
- [ ] Read-only
- [ ] WRITE (Must justify: ...)

## Sign-off
- [ ] I confirm DCO via `git commit -s`

<!-- 10xHunter PRs only (title starts `10xHunter--`); delete otherwise. Checked by the "10x evidence" job.
## 10x Evidence
- Before: <measured value, command, host, n>
- After: <measured value, same scorer and fixture>
- Ratio: <number>x (gate: > 9x)
- Scorer: <exactly what was measured and how>

## 10x Workings
- Card: <link to cards/HUNT-*.md in private_owner/10x-hunter>
- <the arithmetic from Before/After to Ratio, and the floor derivation>
-->
