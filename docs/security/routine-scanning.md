# Routine scanning: cadence, output, failure handling

Part of [security.md](security.md). Status: **spec, not wired** (Mon 14 Sep 2026).

## Principle: match the cadence to how fast the risk changes

- Dependency advisories change **without our code changing**, so they need a daily
  scheduled scan.
- Secrets and code patterns only change when code changes, so they belong in pre-commit
  and PR checks, where the fix is cheapest.
- Nightly re-scans of unchanged code for secrets or SAST findings are wasted runs.

## Cadence

| When | What | Tool | Blocks? |
|---|---|---|---|
| pre-commit | staged secrets, Python patterns, workflow lint | gitleaks, ruff `S`, zizmor | yes (local; agents can skip, so CI repeats it) |
| every PR | new vulns in changed lockfiles; secrets in PR commits; new SAST findings; workflow audit if `.github/**` changed | osv-scanner (PR mode), gitleaks, Semgrep `--baseline-commit`, ruff `S` ratchet, zizmor | yes |
| **daily 06:17 UTC** | full dependency scan of `main` | osv-scanner | no; files or updates the daily issue |
| weekly (Mon) | verified secrets across full history; AI review of high-risk paths; Renovate lockfile maintenance | trufflehog, `/security-review`, Renovate | no; adds to the weekly issue |
| per release (DMG) | signature, notarization, entitlements, updater signature | `codesign --verify --deep --strict`, `spctl -a -vv`, entitlements allowlist diff, minisign verify against the `tauri.conf.json` pubkey | yes, blocks the release |
| quarterly | expired or expiring suppressions; tool version bumps | review of `osv-scanner.toml`, `.gitleaks.toml`, `.github/zizmor.yml` | n/a |

Per-release checks exist because notarization only scans for known malware at submission
time. It does not verify that our entitlements are minimal or that sidecars are signed.

## Output: issues, not the Security tab

SARIF upload to the GitHub Security tab needs GitHub Code Security, which a private repo
on GitHub Free does not have. So:

- The daily job keeps **one rolling issue** titled `security: daily dependency scan`,
  labeled `security`. It edits the issue body with `gh issue edit` rather than opening a
  new issue each day, and closes the issue when the scan is clean.
- The weekly job does the same with `security: weekly review`.
- `gh` CLI, not a marketplace action (one less third-party action; see
  [ci-supply-chain.md](ci-supply-chain.md)).
- Fixing is handed to [autofixing.md](autofixing.md).

## UNKNOWN is never clean

Per `.claude/rules/verification.md`: a scanner that cannot measure must report UNKNOWN.

- osv-scanner exit 127 (error) or 128 (no packages found) fails the job.
- Every job asserts it scanned something: the lockfile count, the file count, the rule
  count.
- Every scanner has a positive-control fixture under `tests/fixtures/security/` that
  MUST produce a finding. A control that passes clean fails the job.
- stderr is never suppressed.

## Workflow skeleton (`.github/workflows/security.yml`)

```yaml
name: security
on:
  pull_request:
  schedule:
    - cron: "17 6 * * *"      # daily, UTC; schedules run on the default branch only
    - cron: "37 6 * * 1"      # weekly, Monday
  workflow_dispatch:
permissions:
  contents: read
jobs:
  deps:
    runs-on: ubuntu-latest    # GitHub-hosted on purpose: see ci-supply-chain.md rule 4
    permissions: { contents: read, issues: write }
    steps:
      - uses: actions/checkout@<sha>  # vX
        with: { persist-credentials: false, fetch-depth: 0 }
      - run: scripts/security/install_scanners.sh osv-scanner   # pinned version + sha256
      - run: scripts/security/scan_deps.sh                      # controls + count assert + scan
      - if: always() && github.event_name != 'pull_request'
        run: scripts/security/report_issue.sh deps              # rolling issue via gh
        env: { GH_TOKEN: "${{ github.token }}" }
  secrets:   { ... gitleaks on PR range; trufflehog on weekly cron ... }
  sast:      { ... semgrep + control fixture ... }
  workflows: { ... zizmor, only when .github/** changed ... }
```

## Local and agent parity

Everything above runs from the CLI with no GitHub dependency, so agents can run the same
checks:

```bash
just security-scan          # all scanners + controls, local, prints a summary table
just security-scan deps     # one area
```

These are `justfile` recipes wrapping `scripts/security/*.sh`, the same scripts CI calls.

## Acceptance criteria

- [if] the daily workflow has not produced a run in 36 hours [then] the existing
  `ci-budget-watch` style watchdog flags it (a silent cron is a failed cron).
- [if] `just security-scan` and the CI job disagree on the finding count for the same SHA
  [then] the scripts have drifted; fail.
- [if] any positive control produces zero findings [then] the job fails with
  `UNKNOWN: <scanner> control did not fire`.
