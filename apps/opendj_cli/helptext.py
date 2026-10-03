"""Library help strings for ``opendj --help`` and text-mode ``--list-verbs``."""

from __future__ import annotations

import json
import sys
from typing import Any

from apps.opendj_cli.verbs import describe_verbs

_LIBRARY_LINES = """\
library (opendj api, LIBM-11; library daemon, not the performance bus):
  opendj api GET /api/v1/playlists
  opendj api POST /api/v1/playlists --json '{"name":"..."}'
  opendj api GET /api/v1/smartlists
  opendj api POST /api/v1/smartlists --json '{"name":"...","rule":{...}}'
  opendj api PUT /api/v1/smartlists/{id} -H 'If-Match: "<etag>"' --json '{"rule":{...}}'
  opendj api DELETE /api/v1/smartlists/{id}
  opendj api GET /api/v1/tracks/{stable_id}
  opendj track key-segments <stable_id>

audio output health (AUDIO-DEVICE-01, issue #923):
  opendj audio_output_health
  opendj audio_switch_output --confirm

feedback pins (FB-20, issue #4085, pin 6af63c5e9b7c):
  opendj feedback comments summary

installed app shell navigation (AGENT-12, issue #2866):
  opendj open performance
  opendj open /performance

installed app PATH (AGENT-05, issue #2751):
  opendj install-cli [--target ~/.local/bin/opendj]   symlink bundled launcher to ~/.local/bin

installed app MCP (AGENT-11, issue #2753):
  opendj status [--json]   lock-file origin, health, build-info (MCP status parity)
  opendj mcp
  claude mcp add opendj -- "/Applications/Open DJ.app/Contents/Resources/payload/bin/opendj" mcp
  MCP tools: status, app_state, command, library, ui_url (browser only),
  open_route (shell), update_check (read-only), update_apply (destructive)

installed app UPDATE (AGENT-13, issue #2942; exits 0 only for current or
update-available, and for an apply that relaunched into the announced build):
  opendj update check                                   # channel status + versions
  opendj update apply [--timeout-s SECONDS]             # install and relaunch"""

LIBRARY_EPILOG = _LIBRARY_LINES

LIST_VERBS_FOOTER = _LIBRARY_LINES


def _verb_flags(row: dict[str, Any]) -> str:
    flags = []
    if row["quick_draw"]:
        flags.append("quick-draw " + ",".join(row["quick_draw"]))
    if row["rampable"]:
        flags.append("--over")
    if row["confirmed_against_mirror"]:
        flags.append("mirror-confirmed")
    return "  ".join(flags)


def print_verbs(as_json: bool) -> None:
    """Print the AGENT-03 bus table, plus library ops in text mode."""
    rows = describe_verbs()
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    name_width = max(len(row["verb"]) for row in rows)
    command_width = max(len(row["command"]) for row in rows)
    usage_width = max(len(row["usage"]) for row in rows)
    print(f"{'verb':<{name_width}}  {'bus command':<{command_width}}  usage")
    for row in rows:
        print(
            f"{row['verb']:<{name_width}}  {row['command']:<{command_width}}  "
            f"{row['usage']:<{usage_width}}  {_verb_flags(row)}".rstrip()
        )
    print()
    print(LIST_VERBS_FOOTER, file=sys.stdout)
