#!/usr/bin/env bash
# Post-checkout workspace clean for the persistent self-hosted runners.
#
# actions/checkout runs with `clean: false` so the dependency trees below
# survive between jobs instead of being deleted and rebuilt on every checkout.
# That churn was a main cause of nucbox's disk I/O saturation (io PSI full
# avg60 23-43%, Thu 10 Sep 2026). Checkout still force-checks-out every
# TRACKED file; this removes every UNTRACKED and IGNORED path except those
# trees, which is exactly what `clean: true` did, minus them.
#
# Kept, and what makes each safe to reuse across branches:
#   node_modules  any depth   `pnpm install --frozen-lockfile` relinks it to the lockfile
#   /.venv        root only   scripts/ci_venv.sh plus an --exact install, or `uv sync`,
#                             makes its contents exactly what a fresh fill would be
#   target        any depth   cargo rebuilds from its own fingerprints
# Everything else goes, including stale build outputs (dist/, .svelte-kit/,
# build/), nested virtualenvs, and the per-run .env.
#
# Pinned by tests/scripts/test_ci_workspace_reuse.py.
set -euo pipefail
git clean -ffdx -e node_modules -e /.venv -e target
