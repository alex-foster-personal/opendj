# Autofixing: who opens the fix PRs

Part of [security.md](security.md). Status: **spec, not wired** (Mon 14 Sep 2026).

## Division of labor (one owner per job, so there are no duplicate PRs)

| Job | Owner | Why |
|---|---|---|
| Vulnerability data | **Dependabot alerts: ON** | Free on private repos. Renovate's `vulnerabilityAlerts` reads them. Currently **disabled** on this repo (the API reported "Dependabot alerts are disabled") |
| Security fix PRs | **Renovate** (`vulnerabilityAlerts` + `osvVulnerabilityAlerts`) | One bot, one PR style, no cooldown on security fixes |
| Version-update PRs | **Renovate** | Grouping, schedules, `minimumReleaseAge`, lockfile maintenance, digest pinning for actions |
| Dependabot security updates / `dependabot.yml` | **OFF** | Would duplicate Renovate's PRs |
| Fixes Renovate cannot do (breaking majors, code changes, pins in `requirements.txt` that conflict) | **Agent fix loop** (below) | Needs code edits and tests |
| Code-pattern fixes (Semgrep, ruff `S`) | the author's agent, in the same PR | Findings block the PR, so they get fixed there |

## Renovate

Use the **Mend-hosted Renovate GitHub App** (free, no infrastructure) rather than
self-hosting `renovatebot/github-action` on our runners. Installing it is an account
setting change for the owner.

### `renovate.json` (spec)

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:recommended", "helpers:pinGitHubActionDigests", ":semanticCommits"],
  "timezone": "Etc/UTC",
  "schedule": ["before 6am on monday"],
  "minimumReleaseAge": "3 days",
  "prConcurrentLimit": 6,
  "dependencyDashboard": true,
  "osvVulnerabilityAlerts": true,
  "vulnerabilityAlerts": {
    "labels": ["security"],
    "schedule": ["at any time"],
    "minimumReleaseAge": null
  },
  "lockFileMaintenance": { "enabled": true, "schedule": ["before 6am on monday"] },
  "platformAutomerge": false,
  "packageRules": [
    {
      "description": "Group non-major updates per ecosystem so a week is ~4 PRs, not 40",
      "matchUpdateTypes": ["minor", "patch"],
      "groupName": "{{manager}} non-major"
    },
    {
      "description": "Automerge only dev-only patch bumps and lockfile refreshes, after checks pass",
      "matchDepTypes": ["devDependencies", "dev"],
      "matchUpdateTypes": ["patch"],
      "automerge": true
    },
    { "matchUpdateTypes": ["lockFileMaintenance"], "automerge": true },
    {
      "description": "Security-sensitive: never automerge, always a human or agent review",
      "matchPackageNames": ["starlette", "fastapi", "uvicorn", "pillow", "rustls", "/^tauri/", "/^@tauri-apps//"],
      "automerge": false,
      "labels": ["security-sensitive"]
    }
  ]
}
```

Notes:

- Managers expected to activate: `pep621` (`pyproject.toml` + `uv.lock`),
  `pip_requirements` (`requirements*.txt`), `npm` (pnpm lockfiles),
  `cargo`, `github-actions`. Verify on the first run's Dependency Dashboard that all five
  appear. If one is missing, the ecosystem is unmanaged.
- `platformAutomerge: false` makes Renovate itself confirm the checks are green before
  merging. Required-status-check enforcement (branch protection) is not available for
  private repos on GitHub Free, so GitHub's native auto-merge would have nothing to wait
  on.
- `requirements.txt` and `uv.lock` must move together. Until CI installs from `uv.lock`
  (see [malicious-packages.md](malicious-packages.md)), a Renovate bump to one without
  the other is a bug the PR check must catch.
- Renovate comments "Artifact update problem" when it changed a manifest but could not
  regenerate the lockfile. That PR must not merge. #2740 did on Mon 14 Sep 2026 and
  left `uv sync --extra dev` unresolvable on main. `.github/workflows/lockfile-check.yml`
  (`uv lock --check`) fails such a PR in seconds. The fleet's merge sweep must also refuse
  it; that needs ADR-0042 follow-up F1, which makes the sweep read completed checks.
- Validate before merging the config:
  `npx --yes --package renovate -- renovate-config-validator`.

## Agent fix loop (for what Renovate cannot do)

When the daily scan issue ([routine-scanning.md](routine-scanning.md)) holds findings
that no Renovate PR covers after 24 hours:

1. The issue is handed to an agent (the IDD fleet via `af-idd-delegate`, or a Claude
   session). Scope is one package per branch.
2. The agent bumps the package, fixes any breakage, and runs the scoped tests
   (`just test-*` for the touched area).
3. **Verify by rescan:** the PR must include osv-scanner output showing the advisory ID
   is gone. This is the "confirm the fix closes the alert" loop that paid tools (Copilot
   Autofix, Snyk Agent Fix) sell, done with tools we already have.
4. Normal merge gate. Security-sensitive packages get a human review.

## Acceptance criteria

- [if] the Dependency Dashboard issue does not list all five managers [then] the setup
  is incomplete.
- [if] Renovate has not opened a PR for a known open advisory (e.g. starlette 1.3.1)
  within 24 hours of the app being installed [then] `vulnerabilityAlerts` is not
  wired; check that Dependabot alerts are enabled.
- [if] an automerged PR ever touches a package matched by the security-sensitive rule
  [then] the config is wrong; revert it and add a regression note.
- [if] an agent fix PR lacks before and after scanner output for the advisory ID [then]
  it is not mergeable.
