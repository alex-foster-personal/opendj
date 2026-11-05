#!/usr/bin/env bash
# ci_node_preflight.sh -- name the runner and the broken precondition BEFORE
# `pnpm install` runs on a self-hosted node job.
#
# Two things went wrong under a running install on Sat 5 Sep 2026 and both
# surfaced as cryptic pnpm errors mid-job: `[ERR_PNPM_ERR_SQLITE_ERROR] disk
# I/O error` (agentbox-13, trunk e14bd2f0a3) and `ERR_PNPM_ENOENT` on a store
# file the index promised (nucbox-wsl-2, trunk 59248abb9). The cause was
# actions/setup-node restoring its cache tarball into the host-shared pnpm
# store; that writer is gone (#1299), and this script is the tripwire that
# says so in one line if the store is ever mutated again, or the disk fills.
#
# A third failure (issue #3153): a reused frontend node_modules whose public
# layer points into a deleted quality-gate-base-* temp tree, or whose .bin is
# empty. `pnpm install --frozen-lockfile` then prints "Already up to date"
# because .pnpm-workspace-state-v1.json is present, and the typing gate /
# production build die with ERR_MODULE_NOT_FOUND / "vite: not found". This
# script probes the public layer when it exists and relinks once.
#
# Checks, each fail-closed and naming the runner:
#   1. `corepack` and `node` resolve on PATH (via ci_runner_preflight.sh)
#   2. at least MIN_FREE_GB free on the filesystem holding the workspace
#   3. if frontend node_modules exists, its public-layer links resolve inside
#      the pnpm store layout; one repair attempt (drop dangling/escaped public
#      links, drop .pnpm-workspace-state-v1.json, frozen reinstall) before
#      failing
#
# `pnpm store status` is deliberately NOT a check here. Measured Sat 5 Sep
# 2026 17:20 UTC on agentbox: a store rebuilt from the lockfile served a fresh
# `pnpm install --frozen-lockfile --offline` in 2.4 s and every node job
# since was green, while `pnpm store status` kept listing all 185 packages
# as modified from every directory it was run in. A probe that reads red on
# a working store is not a probe. The install itself is the store check.
#
# Usage: scripts/ci_node_preflight.sh <frontend-dir>
# Regression lines:
#   - if the disk has under MIN_FREE_GB free then this fails before install,
#     naming the runner, instead of pnpm failing on a random write
#   - if corepack or node is missing then this names it before install
#   - if a poisoned public layer is left in place then the typing gate fails
#     84 times and `pnpm build` dies `vite: not found`
#   - if repair keeps .pnpm-workspace-state-v1.json then pnpm short-circuits
#     "Already up to date" and the tree stays poisoned
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "[ci-node-preflight] usage: ci_node_preflight.sh <frontend-dir>" >&2
    exit 2
fi
readonly FRONTEND_DIR="$1"
readonly MIN_FREE_GB="${MDT_CI_MIN_FREE_GB:-10}"
readonly RUNNER="${RUNNER_NAME:-$(hostname 2>/dev/null || echo unknown)}"
readonly HERE="$(cd "$(dirname "$0")" && pwd)"
readonly WORKSPACE_STATE=".pnpm-workspace-state-v1.json"

"$HERE/ci_runner_preflight.sh" corepack node

# POSIX df (-Pk): GNU --output is not on macOS, where this also has to run.
avail_gb="$(df -Pk "$FRONTEND_DIR" | awk 'NR==2 {print int($4 / 1048576)}')"
if [ "${avail_gb:-0}" -lt "$MIN_FREE_GB" ]; then
    echo "::error::[ci-node-preflight] runner $RUNNER has ${avail_gb:-0}G free under $FRONTEND_DIR, below the ${MIN_FREE_GB}G floor" >&2
    exit 1
fi

# Public-layer package link: relative into this tree's .pnpm store.
# Escaped links (absolute, or ../ into a quality-gate-base-* temp tree) fail.
_is_pnpm_public_target() {
    case "$1" in
        .pnpm/*)
            case "$1" in
                */../*) return 1 ;;
                *) return 0 ;;
            esac
            ;;
        *) return 1 ;;
    esac
}

# .bin entries point at ../<pkg>/... or ../.pnpm/... ; anything that walks
# out of node_modules is poison.
_is_pnpm_bin_target() {
    case "$1" in
        ../*)
            case "$1" in
                ../../*) return 1 ;;
                *)
                    case "${1#../}" in
                        */../*) return 1 ;;
                        *) return 0 ;;
                    esac
                    ;;
            esac
            ;;
        *) return 1 ;;
    esac
}

_list_symlinks() {
    local dir="$1"
    (
        shopt -s nullglob
        for p in "$dir"/*; do
            if [ -L "$p" ]; then
                printf '%s\n' "$p"
            fi
        done
    )
}

_symlink_ok() {
    local link="$1"
    local kind="$2"
    local target
    if [ ! -e "$link" ]; then
        return 1
    fi
    target="$(readlink "$link")" || return 1
    if [ "$kind" = bin ]; then
        _is_pnpm_bin_target "$target"
    else
        _is_pnpm_public_target "$target"
    fi
}

# Fills POISONED_LINKS with public-layer / .bin symlinks to drop.
# Returns 0 if the tree is healthy, 1 if poisoned.
_probe_frontend_node_modules() {
    local nm="$FRONTEND_DIR/node_modules"
    local link poisoned=0
    POISONED_LINKS=()

    while IFS= read -r link; do
        [ -n "$link" ] || continue
        if ! _symlink_ok "$link" public; then
            POISONED_LINKS+=("$link")
            echo "[ci-node-preflight] poisoned link: ${link##*/}" >&2
            poisoned=1
        fi
    done < <(_list_symlinks "$nm")

    if [ -L "$nm/.bin" ]; then
        if ! _symlink_ok "$nm/.bin" public; then
            POISONED_LINKS+=("$nm/.bin")
            echo "[ci-node-preflight] poisoned link: .bin" >&2
            poisoned=1
        fi
    elif [ -d "$nm/.bin" ]; then
        while IFS= read -r link; do
            [ -n "$link" ] || continue
            if ! _symlink_ok "$link" bin; then
                POISONED_LINKS+=("$link")
                echo "[ci-node-preflight] poisoned link: ${link##*/}" >&2
                poisoned=1
            fi
        done < <(_list_symlinks "$nm/.bin")
    fi

    if [ ! -e "$nm/typescript" ]; then
        echo "[ci-node-preflight] missing resolvable node_modules/typescript" >&2
        poisoned=1
    fi

    if [ ! -e "$nm/.bin/vite" ] && [ ! -e "$nm/.bin/vite.cmd" ]; then
        echo "[ci-node-preflight] missing node_modules/.bin/vite" >&2
        poisoned=1
    fi

    if [ "$poisoned" -ne 0 ]; then
        return 1
    fi
    return 0
}

_repair_frontend_node_modules() {
    local link
    if [ "${#POISONED_LINKS[@]}" -gt 0 ]; then
        for link in "${POISONED_LINKS[@]}"; do
            rm -f -- "$link"
        done
    fi
    rm -f -- "$FRONTEND_DIR/node_modules/$WORKSPACE_STATE"
    if ! command -v pnpm >/dev/null 2>&1; then
        echo "::error::[ci-node-preflight] runner $RUNNER: frontend node_modules is poisoned and pnpm is not on PATH" >&2
        exit 1
    fi
    if ! (cd "$FRONTEND_DIR" && pnpm install --frozen-lockfile); then
        echo "::error::[ci-node-preflight] runner $RUNNER: frontend node_modules is still poisoned after reinstall" >&2
        exit 1
    fi
}

if [ -d "$FRONTEND_DIR/node_modules" ]; then
    if ! _probe_frontend_node_modules; then
        echo "[ci-node-preflight] runner $RUNNER: frontend node_modules is poisoned; repairing once" >&2
        _repair_frontend_node_modules
        if ! _probe_frontend_node_modules; then
            echo "::error::[ci-node-preflight] runner $RUNNER: frontend node_modules is still poisoned after reinstall" >&2
            exit 1
        fi
        echo "[ci-node-preflight] runner $RUNNER: repaired frontend node_modules"
    fi
fi

echo "[ci-node-preflight] runner $RUNNER: corepack and node present, ${avail_gb}G free under $FRONTEND_DIR"
