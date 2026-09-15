#!/usr/bin/env bash
# ci_cargo_selfheal.sh -- retry a cargo build ONCE after cleaning a corrupted
# persistent target/ dir, never after anything else.
#
# scripts/ci_clean_untracked.sh keeps target/ across jobs on the self-hosted
# runners on purpose, so cargo can reuse its fingerprints instead of a full
# rebuild. That reuse occasionally reads a half-written .rlib/.so left by a
# job killed mid-write (nucbox disk IO thrash, see afmac
# research/2026-09-14-nucbox-runner-concurrency.md), and cargo reports it as
# a compile error rather than a torn file:
#
#   error[E0786]: found invalid metadata files for crate `time_macros` ...
#     invalid metadata version found: .../target/debug/deps/libtime_macros-*.so
#   error[E0463]: can't find crate for `proc_macro_error`
#
# job 103936733320 (PR #2624, runner nucbox-wsl-12, Mon 14 Sep 2026) failed
# exactly this way -- the corrupt target/ dir, not the PR under test.
#
# This script runs the given cargo command; if it fails AND the failure
# matches one of those two signatures, it warns, runs `cargo clean` scoped to
# ONE profile (never the whole target/ tree, which would also evict every
# other job's warm cache) and retries the command exactly once. Any other
# failure, or a second failure of the same kind, is left red -- masking a
# genuine compile error is worse than the flake this heals.
#
# The E0463 signature is checked against Cargo.lock before it is trusted: a
# crate genuinely absent from Cargo.lock (a real "no such dependency"
# misconfiguration) must stay red, not get retried as if it were corruption.
#
# Usage:
#   scripts/ci_cargo_selfheal.sh <manifest-path> <profile> <command...>
#
#   manifest-path  the Cargo.toml the build is scoped to (its sibling
#                  Cargo.lock is what an E0463 crate name is checked against)
#   profile        the cargo profile to `cargo clean --profile` on detection
#                  (e.g. "dev" for target/debug) -- never the whole target/
#   command        the full build command to run, e.g.
#                  cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --locked
#
# Detection regexes are pinned by tests/scripts/test_ci_cargo_selfheal.py.
set -uo pipefail

if [ "$#" -lt 3 ]; then
    echo "[ci-cargo-selfheal] usage: ci_cargo_selfheal.sh <manifest-path> <profile> <command...>" >&2
    exit 2
fi

readonly MANIFEST="$1"
shift
readonly PROFILE="$1"
shift

manifest_dir="$(dirname "$MANIFEST")"
readonly LOCKFILE="${manifest_dir}/Cargo.lock"
readonly RUNNER="${RUNNER_NAME:-$(hostname 2>/dev/null || echo unknown)}"

# What could satisfy this check without satisfying its intent? A build that
# fails for an unrelated reason but happens to echo one of these substrings
# from somewhere else (a test's own stdout, a doc comment). Both signatures
# are anchored on cargo's own `error[Exxxx]:` line, not free text, and E0463
# is additionally required to name a crate Cargo.lock says should resolve.
is_corrupted_target() {
    local log="$1"
    if grep -qE '^error\[E0786\]: found invalid metadata files' "$log"; then
        return 0
    fi

    local crate
    crate="$(grep -oE "^error\[E0463\]: can't find crate for \`[^\`]+\`" "$log" \
        | head -n1 \
        | sed -E "s/.*\`([^\`]+)\`.*/\1/")"
    if [ -n "$crate" ] && grep -qE "^name = \"${crate}\"\$" "$LOCKFILE"; then
        return 0
    fi

    return 1
}

log="$(mktemp)"
trap 'rm -f "$log"' EXIT

"$@" 2>&1 | tee "$log"
status="${PIPESTATUS[0]}"

if [ "$status" -eq 0 ]; then
    exit 0
fi

if ! is_corrupted_target "$log"; then
    exit "$status"
fi

echo "::warning::runner ${RUNNER} hit a corrupted cargo target/ dir (manifest ${MANIFEST}) -- cleaning profile '${PROFILE}' and retrying the build once"
cargo clean --manifest-path "$MANIFEST" --profile "$PROFILE"
"$@"
exit "$?"
