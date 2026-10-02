#!/usr/bin/env bash
# The e2e output-topology gate: run it, skip it, or refuse.
#
# Usage: ci_output_topology_gate.sh <frontend dir>
#
# The feature is three files: the topology source, its playwright spec and
# that spec's config. The step landed on main before the feature (PR #3837),
# so a tree may hold none of them.
#
#   none present  -> print SKIPPED and exit 0: there is no feature to gate
#   all present   -> run the gate; its exit code is this script's
#   anything else -> exit 1 naming what is missing
#
# The skip is keyed on the whole feature, never on the config alone: deleting
# or renaming the config or the spec while the source stays is a failure, not
# a quiet way to switch the gate off. Moving the source without updating this
# list fails the same way, because the config and spec are still here.
#
# Regression lines (tests/scripts/test_preview_control_plane_first.py):
#   - if a tree with none of the feature fails then main's e2e is red
#   - if a tree with part of the feature skips then a rename disables the gate
#   - if a red playwright run exits 0 then the gate is decoration
set -euo pipefail

frontend="${1:?usage: ci_output_topology_gate.sh <frontend dir>}"
cd "$frontend"

config=tests/e2e/playwright.audio-output-topology.config.ts
present=0
missing=""
for feature_file in src/lib/rb/audio-output-topology.ts "$config" tests/e2e/audio-output-topology.spec.ts; do
  if [ -f "$feature_file" ]; then
    present=$((present + 1))
  else
    missing="${missing}missing: ${feature_file}"$'\n'
  fi
done

if [ "$present" -eq 0 ]; then
  echo "SKIPPED: the output-topology feature is not in this tree (no source, spec or config), so its gate did not run"
  exit 0
fi
if [ -n "$missing" ]; then
  echo "FAIL: the output-topology feature is only partly in this tree, so its gate cannot run" >&2
  printf '%s' "$missing" >&2
  exit 1
fi
exec pnpm exec playwright test --config "$config" --output test-results/audio-output-topology
