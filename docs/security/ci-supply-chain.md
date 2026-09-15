# CI and GitHub Actions supply chain

Part of [security.md](security.md). Status: **spec, not wired** (Mon 14 Sep 2026).

## Why this matters here

Security scanners are themselves an attack surface. On Thu 19 Mar 2026, 76 of 77
`aquasecurity/trivy-action` tags were force-pushed to a credential stealer that dumped
runner memory and harvested SSH and cloud keys, specifically on **self-hosted** runners
(CVE-2026-33634). Checkmarx's KICS actions were hit the same week. Details and sources:
[../research/security-paid-vs-oss-20260914.md](../research/security-paid-vs-oss-20260914.md).

Our exposure, measured Mon 14 Sep 2026:

- **45 self-hosted runners** (`agentbox*` and `nucbox-wsl*`), selected
  via the repo variables `CI_RUNS_ON_LINUX` / `CI_RUNS_ON_E2E` /
  `CI_RUNS_ON_UNPRIVILEGED_LINUX`.
- **6 workflows without a top-level `permissions:` block**, so they get the default
  token scope: `ci.yml`, `full-ci.yml`, `macos-native-companion.yml`,
  `macos-packaging.yml`, `release-check.yml`, `windows-parity.yml`.
- Actions sampled in `ci.yml` are already SHA-pinned (good).

## Rules

1. Every workflow declares top-level `permissions: contents: read`. Jobs widen
   individually (e.g. `issues: write` for the scan-report job).
2. Every third-party action is pinned to a full commit SHA with a version comment.
   Renovate keeps the pins fresh (`helpers:pinGitHubActionDigests`, see
   [autofixing.md](autofixing.md)).
3. No `pull_request_target`, and no `${{ github.event.* }}` interpolated into `run:`
   (pass values through `env:`, as the repo already does).
4. **Security scanners run on GitHub-hosted runners** with no repo secrets in scope. They
   need no secrets, and a compromised scanner on a GitHub-hosted runner steals nothing
   persistent. Prefer a checksum-verified release binary over a marketplace action.
   Cost is small: osv-scanner, gitleaks and zizmor each run in about a minute.
5. Signing material (Apple Developer ID / notary profile, Tauri updater minisign key)
   never lives on self-hosted runners that also run PR jobs. Today signing is a local
   step (`scripts/sign_macos_developer_id.sh`). Keep it that way until a dedicated,
   isolated release runner exists.
6. `actions/checkout` uses `persist-credentials: false` in any job that does not push.

## Tool: zizmor

Static analysis for GitHub Actions: template injection, over-broad permissions,
unpinned actions, credential persistence, dangerous triggers. v1.30.1 on Wed 9 Sep 2026.
Detect-only (no autofix).

```bash
uvx zizmor --min-severity=medium .github/workflows
```

- pre-commit: `zizmorcore/zizmor-pre-commit` (rev pinned to the same version).
- PR check: run when `.github/**` changes. Config (if needed) at `.github/zizmor.yml`,
  where every disabled audit carries a reason.

## Acceptance criteria

- [if] any `.github/workflows/*.yml` lacks a top-level `permissions:` key [then] a lint
  step fails. Invariant check: `grep -L '^permissions:'` must print nothing.
- [if] zizmor reports any finding at medium or above on main [then] fail.
- [if] a control workflow under `tests/fixtures/security/zizmor-control/` containing
  `run: echo ${{ github.event.issue.title }}` does not make zizmor exit non-zero
  [then] fail: the tool is not auditing.
- [if] a security-scan job's `runs-on` resolves to a self-hosted label [then] fail
  (checked by a grep over `security.yml`).
