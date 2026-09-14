# Malicious and compromised packages (cooldowns and install-time controls)

Part of [security.md](security.md). Status: **partly in place, rest is spec** (Mon 14 Sep 2026).

## What it covers

A legitimate package version that is **poisoned at publish time**: a hijacked maintainer
account, a worm (the Shai-Hulud family across npm and PyPI, 2025-2026), a hijacked token
(LiteLLM on PyPI, Tue 24 Mar 2026, whose `.pth` payload ran on every Python start).
There is no advisory yet, so [dependency-scanning.md](dependency-scanning.md) cannot see
it.

The cheap, standard defense is a **release-age cooldown**. Almost every poisoned version
in 2025-2026 was pulled within 40 minutes to 12 hours. Refusing versions younger than a
few days avoids them passively. Security fixes bypass the cooldown (they go through
Renovate `vulnerabilityAlerts`; see [autofixing.md](autofixing.md)).

Cooldowns do not stop slow-burn typosquats (malicious crates sat on crates.io for four
months in 2025). Behavioral scanners (Socket, Aikido Safe Chain) cover that; they are
deferred to the store launch (see [security.md](security.md#paid-tools)).

## Current state (measured on main `8842bbf50`)

| Control | State |
|---|---|
| pnpm lifecycle scripts | **In place.** Both `pnpm-workspace.yaml` files use `allowBuilds` (esbuild allowed; edgedriver and geckodriver denied in desktop). pnpm 10+ blocks other dependency build scripts by default |
| pnpm release-age cooldown | Implicit only: pnpm 11 defaults `minimumReleaseAge` to 1 day, but no `packageManager` pin exists, so the effective default depends on whichever pnpm a machine runs (11.22.0 locally) |
| uv `exclude-newer` | **Not set** |
| CI Python install | **Gap.** `ci.yml` runs `pip install -r requirements.txt`, and 37 of its 46 requirements are floors or ranges (e.g. `Pillow>=10.0`), so every CI run resolves the *newest* release at install time, with no cooldown, on self-hosted runners |
| Cargo | `Cargo.lock` committed; changes only when bumped |

## Setup spec

### 1. CI installs from the lockfile

Make CI install from `uv.lock` (`uv sync --frozen --extra dev`), or generate
`requirements.txt` from it (`uv export --frozen --no-hashes`) and fail CI if the two
differ. Either way, CI then installs exactly the reviewed versions, and the cooldown
below becomes effective. The `interpreter-check` and conftest guards in `CLAUDE.md`
already handle the `uv run` traps.

### 2. uv cooldown

```toml
[tool.uv]
exclude-newer = "3 days"   # relative durations need uv >= 0.9.17; local is 0.12.4, CI setup-uv installs latest
```

Per-package escape hatch for a vetted urgent fix: `--exclude-newer-package` (confirmed in
`uv help lock`). Record every use in the PR body.

### 3. pnpm cooldown, explicit

In both `pnpm-workspace.yaml` files:

```yaml
minimumReleaseAge: 4320        # minutes = 3 days; explicit, not version-dependent
minimumReleaseAgeExclude: []   # vetted exceptions only; keep the existing storybook pins
```

Also add `"packageManager": "pnpm@11.x.y"` to both `package.json` files so every machine
and runner uses the same pnpm major. pnpm 11 removed the old `onlyBuiltDependencies`
keys, so a stray pnpm 10 would silently ignore `allowBuilds`.

### 4. Renovate

`"minimumReleaseAge": "3 days"` for all version updates (already in the
[autofixing.md](autofixing.md) spec). Security PRs are exempt.

## Acceptance criteria

- [if] `uv lock --dry-run` with a control package whose newest release is under 3 days
  old selects that newest release [then] `exclude-newer` is not active.
- [if] CI's installed versions (`pip freeze` or `uv pip list`) differ from `uv.lock`
  for any package [then] fail: CI is not installing the reviewed set.
- [if] `pnpm config get minimumReleaseAge` in either app dir is not `4320` [then] fail.
- [if] a dependency not listed in `allowBuilds` runs a lifecycle script during
  `pnpm install` [then] fail (pnpm reports ignored builds; assert the list is empty or
  expected).
