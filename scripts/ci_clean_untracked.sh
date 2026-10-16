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

# The linked odj-audio binaries in the kept target dir are runnable without a
# build: apps/shared/odj_audio_binary.py falls back to the newest
# apps/audio-engine/target/{release,debug}/odj-audio, so a job that never runs
# cargo would test whatever branch last built on this runner (agbox2-6, Fri 9
# Oct 2026: another branch's binary made a full-scale sine read 0.486). Only
# the linked binaries go; cargo relinks them from its own fingerprints.
# Plain shell on purpose: this step runs before setup-python and before the
# stale-install guard, so it must launch no interpreter
# (tests/scripts/test_ci_workflow_stale_install_coverage.py, R2).
# A kept target dir, or a profile dir in it, left as a symlink by an earlier job
# would make the rm below follow it and delete a binary outside the checkout.
# Unlink each such symlink (never its contents) before touching any binary.
for dir in apps/audio-engine/target apps/audio-engine/target/release apps/audio-engine/target/debug; do
  if [ -L "$dir" ]; then
    rm -f -- "$dir"
    echo "[ci-clean] removed symlinked engine dir $dir"
  fi
done
for profile in release debug; do
  for name in odj-audio odj-audio.exe; do
    bin="apps/audio-engine/target/$profile/$name"
    if [ -f "$bin" ] || [ -L "$bin" ]; then
      rm -f -- "$bin"
      echo "[ci-clean] removed stale engine binary $bin"
    fi
  done
done
