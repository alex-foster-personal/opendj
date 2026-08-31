#!/usr/bin/env bash
# Pointer stub: the real runner lives with its skill.
#   .agents/skills/ingest-new-tracks/scripts/ingest_tracks.sh
# Kept here so `scripts/` stays the single place to look for entry points.
exec "$(dirname "$0")/../.agents/skills/ingest-new-tracks/scripts/ingest_tracks.sh" "$@"
