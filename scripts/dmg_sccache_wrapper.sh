#!/usr/bin/env bash
#
# RUSTC_WRAPPER for `just dmg` only. Cargo invokes this as:
#   $RUSTC_WRAPPER rustc <args>
#
# Tries the sccache captured by scripts/dmg_sccache_env.sh (absolute path,
# so the cargo subshell's PATH=/usr/bin:$PATH cannot hide Homebrew). If
# sccache itself fails -- corrupt cache, dead server -- exec rustc so the
# dmg still builds.
#
# -Claude
set -u

if [ -n "${MDT_SCCACHE_BIN:-}" ] && [ -x "${MDT_SCCACHE_BIN}" ]; then
    if "${MDT_SCCACHE_BIN}" "$@"; then
        exit 0
    fi
    echo "[WARN] sccache failed on this rustc invocation; falling back to plain rustc" >&2
fi
exec "$@"
