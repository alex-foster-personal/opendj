#!/usr/bin/env bash
# Re-point one branch onto the rewritten history after the going-public
# filter-repo pass (music-dj-tools OSS-REWRITE-01, issue #911, ADR 10).
#
# Usage:
#   scripts/rewrite_rebase.sh <branch> <commit-map> [old-main-sha]
#
#   <branch>      local branch to re-point (checked out or not)
#   <commit-map>  filter-repo's .git/filter-repo/commit-map: "old new" per line
#   old-main-sha  the pre-rewrite main tip; default: first line's old SHA
#                 that is also an ancestor of the branch is found automatically
#
# What it does, and refuses to do:
#   1. Finds the branch's merge-base with OLD main, looks it up in the map.
#   2. REFUSES if any commit on the branch touches an excised path: rebasing
#      would re-introduce the very files the rewrite removed. Those branches
#      must be run through filter-repo with the same path list instead.
#   3. git rebase --onto <new base> <old base> <branch>.
#   4. Proves the result: the branch's patch against NEW main must be
#      byte-identical to its patch against OLD main. A rebase that changed
#      the diff is not a re-point, it is a different change.
set -euo pipefail

branch="${1:?branch}"; map="${2:?commit-map path}"
[[ -f "$map" ]] || { echo "[ERROR] commit-map not found: $map" >&2; exit 2; }

# Paths removed by the rewrite. Keep in step with issue #911 step 3.
EXCISED_RE='^(\.planning/bifrost2-handoff/|tests/fixtures/rekordbox/|tests/fixtures/rb-usb-export/|tests/fixtures/rb-usb-export-onetera-20260805/|docs/controller/reference/|docs/threads/|handoffs/|\.planning/(MAINTAINER-QUEUE\.md|MERGE-QUEUE\.md|todos/|strategy-brain/|seeds/)|tools/deck-diagrams/devices/[^/]+/(source/|midi-list-page-))'

map_new() { awk -v o="$1" '$1==o{print $2; exit}' "$map"; }

# 1. old base: the newest ancestor of the branch that appears in the map.
old_base=""
while read -r sha; do
  if [[ -n "$(map_new "$sha")" ]]; then old_base="$sha"; break; fi
done < <(git rev-list --first-parent "$branch" | head -500)
[[ -n "$old_base" ]] || { echo "[ERROR] no ancestor of $branch is in the commit-map; is this branch already rewritten?" >&2; exit 2; }
new_base="$(map_new "$old_base")"

# 2. refuse branches that touch excised paths.
touched="$(git diff --name-only "$old_base" "$branch" | grep -E "$EXCISED_RE" || true)"
if [[ -n "$touched" ]]; then
  echo "[ERROR] $branch modifies excised paths; run filter-repo on it, do not rebase:" >&2
  echo "$touched" | sed 's/^/    /' >&2; exit 3
fi

# 3. the re-point, with the pre-image kept for the proof.
before="$(git diff "$old_base" "$branch" | git hash-object --stdin)"
echo "[rebase] $branch: --onto ${new_base:0:8} ${old_base:0:8}"
git rebase --quiet --onto "$new_base" "$old_base" "$branch"

# 4. proof: identical patch against the new base.
after="$(git diff "$new_base" "$branch" | git hash-object --stdin)"
if [[ "$before" != "$after" ]]; then
  echo "[ERROR] patch changed during rebase ($before -> $after); inspect before pushing" >&2; exit 4
fi
echo "[OK] $branch re-pointed; patch byte-identical (blob $before). Push with: git push --force-with-lease origin $branch"
