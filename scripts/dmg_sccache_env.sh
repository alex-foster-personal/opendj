#!/usr/bin/env bash
#
# Source from `just dmg` only. Arms sccache for that cargo invocation when
# the binary is healthy; otherwise leaves cargo unwrapped and says so.
#
# WHY THIS EXISTS. The Air's cold `cargo build --release` is 8m55s and
# recurs on every fresh worktree because each worktree has its own
# `target/`. sccache with a cache directory OUTSIDE every worktree is the
# only lever: the dmg cannot leave macOS (codesign / notary / stapler live
# in the Air's login keychain). A GPU buys nothing; a Linux-to-macOS
# cross-compile needs the macOS SDK and still has to come back to be signed.
#
# SCOPE. RUSTC_WRAPPER is set here and unset by the recipe after cargo
# tauri build. Other recipes (dev-attach, tests) never source this file, so
# an unexpected wrapper cannot silently affect them.
#
# FALLBACK, NEVER FAIL. A missing binary, a cache that cannot be created,
# a cache directory inside this worktree, or `sccache --show-stats` failing
# all unset the wrapper and print that the build is falling back to plain
# cargo. A corrupt cache must not fail the dmg.
#
# PATH TRAP. The cargo subshell prefixes PATH with /usr/bin so Apple's
# xattr wins over Homebrew. RUSTC_WRAPPER is therefore an absolute path
# (the wrapper, which itself holds the absolute sccache captured here)
# rather than the bare name `sccache`, which that subshell would not find.
#
# CACHE BOUND. 2G. sccache's default is 10G; the Air SSD is chronically
# near full. Override with MDT_SCCACHE_CACHE_SIZE. Directory override:
# MDT_SCCACHE_DIR, which is still refused if it sits inside this worktree.
#
# Usage:
#   . scripts/dmg_sccache_env.sh     source from just dmg
#   scripts/dmg_sccache_env.sh       usage error (exit 2)
#
# -Claude

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "[ERROR] source this file from just dmg; do not execute it" >&2
    exit 2
fi

_mdt_sccache_self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_mdt_sccache_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
_mdt_sccache_root="${_mdt_sccache_root%/}"

unset MDT_SCCACHE_ACTIVE || true
unset MDT_SCCACHE_BIN || true
unset RUSTC_WRAPPER || true

_mdt_sccache_fallback() {
    local reason="$1"
    unset RUSTC_WRAPPER || true
    unset MDT_SCCACHE_ACTIVE || true
    unset MDT_SCCACHE_BIN || true
    unset CARGO_INCREMENTAL || true
    echo "[WARN] sccache unavailable ($reason); falling back to a plain cargo build" >&2
    return 0
}

_mdt_sccache_inside_worktree() {
    local dir="$1"
    local root="$2"
    [ -n "$root" ] || return 1
    dir="${dir%/}"
    [ "$dir" = "$root" ] && return 0
    case "$dir" in
        "$root"/*) return 0 ;;
    esac
    return 1
}

if [ -n "${MDT_SCCACHE_DIR:-}" ]; then
    SCCACHE_DIR="$MDT_SCCACHE_DIR"
elif [ "$(uname -s)" = Darwin ]; then
    SCCACHE_DIR="${HOME}/Library/Caches/opendj-sccache"
else
    SCCACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/opendj-sccache"
fi
export SCCACHE_DIR
export SCCACHE_CACHE_SIZE="${MDT_SCCACHE_CACHE_SIZE:-2G}"

if [ -n "$_mdt_sccache_root" ]; then
    export SCCACHE_BASEDIRS="$_mdt_sccache_root"
fi

if _mdt_sccache_inside_worktree "$SCCACHE_DIR" "$_mdt_sccache_root"; then
    _mdt_sccache_fallback "SCCACHE_DIR $SCCACHE_DIR is inside this worktree"
    return 0
fi

if ! command -v sccache >/dev/null 2>&1; then
    _mdt_sccache_fallback "sccache not on PATH; brew install sccache"
    return 0
fi

if ! mkdir -p "$SCCACHE_DIR" 2>/dev/null; then
    _mdt_sccache_fallback "cannot create $SCCACHE_DIR"
    return 0
fi

_mdt_sccache_bin="$(command -v sccache)"
if ! "$_mdt_sccache_bin" --show-stats >/dev/null 2>&1; then
    _mdt_sccache_fallback "sccache --show-stats failed (cache corrupt or server unusable)"
    return 0
fi

export MDT_SCCACHE_BIN="$_mdt_sccache_bin"
export RUSTC_WRAPPER="$_mdt_sccache_self/dmg_sccache_wrapper.sh"
export MDT_SCCACHE_ACTIVE=1
export CARGO_INCREMENTAL=0
echo "[INFO] sccache: RUSTC_WRAPPER=$RUSTC_WRAPPER (sccache at $MDT_SCCACHE_BIN) SCCACHE_DIR=$SCCACHE_DIR SCCACHE_CACHE_SIZE=$SCCACHE_CACHE_SIZE (this cargo invocation only)"
