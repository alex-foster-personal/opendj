#!/usr/bin/env bash
# Pointer stub: the real runner lives with its skill.
#   .agents/skills/virgin-boot/scripts/virgin_boot.sh
# Kept here so `scripts/` stays the single place to look for entry points.
set -euo pipefail
exec "$(dirname "$0")/../.agents/skills/virgin-boot/scripts/virgin_boot.sh" "$@"
