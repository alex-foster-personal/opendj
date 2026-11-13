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
  opendj density                                         # read persisted library density
  opendj density compact                                 # compact library rows
  opendj density cosy                                    # cosy library rows

MyTag edits (same undoable routes as the library modal):
  opendj track mytag-rename OLD NEW [--merge] [--undo-session SESSION] [--json]
  opendj track mytag-delete NAME --confirm [--undo-session SESSION] [--json]
  Delete removes the tag from every track. Server refusals return a nonzero exit code.

play history (PLAYS-10523, issue #9745; Open DJ play log only, one list per night from 06:00 local):
  opendj history list [tz=ZONE]
  opendj history show <YYYY-MM-DD> [tz=ZONE]
  opendj history save-playlist <YYYY-MM-DD> [name=NAME] [tz=ZONE]
  opendj history delete-play <stable_id> <play_id>
  opendj history delete-day <YYYY-MM-DD> [IANA_ZONE]

recorder recovery (SET-12844; status-derived stale owner only):
  opendj rec recover

audio output health (AUDIO-DEVICE-01, issue #923):
  opendj audio_output_health
  opendj audio_switch_output --confirm
  opendj audio_reconnect_speaker --confirm

top-bar performance card rows (PERFREADOUT-12579, read-only):
  opendj perf_readouts

ingest drop staging (sync export):
  opendj ingest stage PATH [PATH ...] [--batch B] [--force] [--no-refresh] [--dry-run]

ingest enrichment (ENRICH-09):
  opendj enrichment status [--batch B]
  opendj enrichment rerun --batch B | --playlist P

store cover suggestions (LIBM-189; accept only covers for tracks you own and bought there):
  opendj artwork list
  opendj artwork accept --stable-id S ... | --all
  opendj artwork skip --stable-id S ... | --all

lighting tools (LIGHT-70, LIGHT-10573):
  opendj lighting monitor [--artnet] [--sacn] [--host H] [--sacn-universe U ...] [--json]
  opendj lighting replay FILE [--at S] [--json]  (same StageQuery as window.musicDjToolsLightingStage)
  opendj lighting scenes [--json]  (GET /api/v1/lighting/scenes)
  opendj lighting look [--json]  (GET /api/v1/lighting/look)
  opendj lighting set palette cool|warm|vivid|contrast [--json]  (PUT /api/v1/lighting/settings/KEY)

hotkeys overlay rows (#12287; id, chord, description; rows behind an off build flag say unavailable):
  opendj hotkeys [--platform mac|other] [--json]

MIDI monitor (each incoming message with its mapped action or unmapped, then fader direction):
  opendj midi monitor --device <name-match> [--seconds N]

session played set (SESSION-PLAYED-01; the green library rows, kept across crashes and relaunches):
  opendj session played | resume | new

crowd requests (CROWDREQ-10749; catalog session, request list, dismiss and loaded):
  opendj requests open --playlist <id> [--text] [--cooldown 180]
  opendj requests close | list [--status new] | dismiss <id> | loaded <id> | clear

now playing for streamers and overlays (NOWP-11489; the track the room hears, 3 s settle):
  opendj now-playing [--json] [--watch] [--interval S]

desktop window geometry (WINSTATE-01; per monitor, survives updates) and extra windows:
  opendj window state | reset
  opendj window open|close|focus second-screen

phone remote LAN listener (REMOTE-CTL; pairing code and paired devices):
  opendj remote start [--port N] | stop | status | rotate | forget <device-id>
  opendj remote qr [--target remote|request|display] [--out FILE]

playlist art thumbnail (ARTTHUMB-8728b) and tab-separated text track list (LIBORG-10314):
  opendj playlist thumbnail <playlist-id> [--size 64|232] [--out FILE]
  opendj playlist export <playlist-id> --format txt [--out FILE]

Tag List prep list (issue #10028; up to 100 tracks, rekordbox Tag List):
  opendj taglist list | add <stable_id>... | remove <stable_id> | clear | save <name>

cloud offload (OFFLOAD: free disk, keep the row, get it back any time):
  opendj offload tracks <stable_id>... [--wait] | playlist <playlist_id> [--wait]
  opendj offload restore <stable_id>... --for-24h|--permanent [--wait]
  opendj offload restore-playlist <playlist_id> --for-24h|--permanent [--wait]
  opendj offload status <stable_id>... | summary | op <op_id> | ops | cancel <op_id>
  opendj offload sweep [--dry-run] [--wait]

feedback pins (FB-20, issue #4085, pin 6af63c5e9b7c):
  opendj feedback comments summary

browser errors (AGENT-8304; MCP tool client_errors returns the same document):
  opendj client_errors [--since ISO] [--build-sha PREFIX] [--kind KIND] [--contains TEXT] [--limit N] [--untriaged]

installed app shell navigation (AGENT-12, issue #2866):
  opendj open performance
  opendj open /performance
  opendj open browser            open the UI in the default browser (UIURL-02)

installed app PATH (AGENT-05, issue #2751):
  opendj install-cli [--target ~/.local/bin/opendj]   symlink bundled launcher to ~/.local/bin

installed app MCP (AGENT-11, issue #2753):
  opendj status [--json]   lock-file origin, health, build-info (MCP status parity)
  opendj mcp
  claude mcp add opendj -- "/Applications/Open DJ.app/Contents/Resources/payload/bin/opendj" mcp
  MCP tools: status, app_state, command, library, ui_url (browser only),
  open_route (shell), update_check (read-only), update_apply (destructive),
  client_errors (read-only, AGENT-8304)
  app_update_ready, app_update_status (read-only), app_update_install (destructive),
  app_update_cancel
  AutoDJ MCP tools (AGENT-29): get_mix_state, get_track_profile, get_mix_windows,
  score_pair, suggest_next, list_verbs, list_transition_styles,
  propose_transition, validate_plan, show_plan, schedule_plan, amend_plan,
  plan_set, show_set, schedule_set_next

AutoDJ (AGENT-29, JSON out; plans continue a playing set, never start one):
  opendj dj state                                       # bar, section, mixer per deck
  opendj dj profile|windows|score|suggest ...           # track facts and fit
  opendj dj propose <in_stable_id> --intent "wind down" # validated plans
  opendj dj validate|schedule <plan.json|plan.mix|->    # check, then perform
  opendj dj show <plan.mix|plan.json> [--as grid|json]  # mix score / bar grid, offline
  opendj dj amend <plan_id> abort|finish_now|extend [--bars N]
  opendj dj set-plan <id> <id> ... [--intent TEXT]      # a whole set, one transition per pair
  opendj dj set-show <set.mix> [--as grid|json]         # set score / one grid per transition
  opendj dj set-next <set.mix>                          # perform the transition due now
  opendj dj try --out <id> --in <id> [-n 6] [--context-bars 4] [--json]  # render + rank candidates
  opendj dj try show <job_id>                           # the job, its pick and its current etag
  opendj dj try pick <job_id> <cand> --etag <etag>      # record the chosen transition (etag from try show)
  opendj dj try undo <job_id> --etag <etag>             # remove that pick (etag from try show)
  opendj dj try lanes <job_id> <cand> [--columns 800]   # the panel's lane document
  opendj dj try save-wav <job_id> <cand> --out <new.wav> # the WAV the panel auditions

installed app UPDATE (AGENT-13, issue #2942; exits 0 only for current or
update-available, and for an apply that relaunched into the announced build):
  opendj update check                                   # channel status + versions
  opendj update apply [--timeout-s SECONDS]             # install and relaunch

restart to update a LOCAL build (INSTALL-39; the tray shows "Restart to update"):
  opendj update ready <App.app.tar.gz> [--notes TEXT]   # flag a signed build as pending
  opendj update status                                  # what is pending, and is it running
  opendj update install [--timeout-s SECONDS]           # 10 s countdown, then shell verifies, installs, relaunches
  opendj update cancel                                  # withdraw it during the countdown (or Esc Space x4 in the app)"""

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
