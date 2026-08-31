#!/usr/bin/env bash
# Pointer stub: the real runner lives with its skill.
#   .agents/skills/ship-dmg/scripts/ship_dmg.sh
# Kept here so `scripts/` stays the single place to look for entry points.
exec "$(dirname "$0")/../.agents/skills/ship-dmg/scripts/ship_dmg.sh" "$@"
