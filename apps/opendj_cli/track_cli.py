"""``opendj track <verb>``: the CLI's track-analysis surface.

Unlike every other ``opendj`` head, ``track`` never touches the AGENT-03
engine bus. ``key-segments`` reads straight from the state store, the same
read `/anlz` does, so an agent (or a human) can inspect one track's analysis
without a running deck engine. The ``grid-*`` verbs (GRIDFLAG-03) are the
exception to "no engine": they are the CLI twin of ``/api/v1/beatgrid-flags``
and call the running engine over HTTP, because the scan and the dismissal
write state the engine owns.

``opendj track key-segments <stable_id>`` is nav1-key-record's own scope-lock
line ((private path) item 3): it must
print the SAME `key_segments` block the `/anlz` payload carries, reusing the
identical overlay function rather than a second read of the record.

Issue #3037: this CLI is the one caller in the repo that always intends a
CONCRETE state DB path, so unlike ``state_conn_ro``'s own generic contract
(``None`` means "no own record", a legitimate state -- its docstring is load-
bearing, read it before touching that function), this module resolves a real
path up front and treats that path's absence as its own distinct failure,
never folded into "track not found". See ``_resolve_state_db``.

-Claude
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx

from apps.opendj_cli import EXIT_FAILED, EXIT_NO_ENGINE, api_cli
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail
from apps.opendj_cli.track_comment import TrackCommentError, engine_client, set_track_comment
from apps.opendj_cli.track_tracklist_hidden import set_tracklist_hidden
from apps.webui.server.rb_vendor_pkg.own_lane_store import state_conn_ro

KEY_SEGMENTS_VERB = "key-segments"
#: GRIDFLAG-03: the CLI twin of `/api/v1/beatgrid-flags` (the Err column's
#: beatgrid flag). These three talk to the running engine over HTTP, because
#: the scan and the dismissal are the engine's to do; they print its JSON.
GRID_FLAGS_VERB = "grid-flags"
GRID_SCAN_VERB = "grid-scan"
GRID_FLAG_VERB = "grid-flag"
#: GRIDEDIT-6235d: the CLI twin of the deck's grid adjust / grid shift buttons,
#: over `/api/v1/tracks/{id}/grid-edit` on the running engine.
GRID_EDIT_VERB = "grid-edit"
_GRID_EDIT_SOURCES = ("rekordbox", "own")
#: GRID-OCTAVE-02: the CLI twin of the BPM hover card's "use the other tempo",
#: over `/api/v1/tracks/{id}/bpm-pick`.
BPM_PICK_VERB = "bpm-pick"
_GRID_FLAGS_PATH = "/api/v1/beatgrid-flags"
#: SRCFLAG-01..03: the CLI twin of `/api/v1/source-flags` (flag-only source checks).
SOURCE_FLAGS_VERB = "source-flags"
_SOURCE_FLAGS_PATH = "/api/v1/source-flags"
_SOURCE_FLAG_CODES = ("all", "length_mismatch", "stems_other_recording", "hardlinked_write")
#: GRIDFIT-03: the CLI twin of the grid-vs-audio routes under the same prefix.
GRID_FIT_VERB = "grid-fit"
GRID_FIT_SCAN_VERB = "grid-fit-scan"
_FIT_FILTERS = ("findings", "wrong_bpm", "off_beat", "shifted", "no_verdict", "fit", "unknown", "all")
#: AUTOGAIN-06: the CLI twin of `/api/v1/auto-gain` (the deck's AG readout).
AUTO_GAIN_VERB = "auto-gain"
_AUTO_GAIN_PATH = "/api/v1/auto-gain/tracks"
#: AGENT-51: the CLI twin of `/api/v1/tracks/{id}/transition-in` (the deck
#: waveform's "Transition in around here"). The engine snaps to its own grid.
TRANSITION_IN_VERB = "transition-in"
#: Issue #8339: the CLI twin of `/api/v1/tracks/{id}/tempo-stability` (the deck
#: strip's tempo lane and the waveform's tempo-change marks).
TEMPO_VERB = "tempo"
#: LIBUX-56: the CLI twin of the library's Comment cell editor (double-click
#: the cell, or right-click a row and choose Edit comment).
COMMENT_VERB = "comment"
#: NOWP-11487: the CLI twin of the library Hide in tracklists context menu.
TRACKLIST_HIDDEN_VERB = "hide-in-tracklists"
#: LIBUX-10046: the CLI twin of the library's Related tab and its criteria cog.
RELATED_VERB = "related"
_RELATED_FLAGS = ("key", "genre", "color", "rating", "mytag", "artist")
_GRID_CLASSES = ("flagged", "suspect", "variable_tempo", "unknown", "ok", "all")
#: QLTSCAN-04: the CLI twin of `/api/v1/audio-quality` (the QLT column's
#: letter). Same rule as the grid verbs: the engine owns the scan, so these
#: call it over HTTP and print its JSON.
QUALITY_VERB = "quality"
QUALITY_SCAN_VERB = "quality-scan"
QUALITY_OF_VERB = "quality-of"
_QUALITY_PATH = "/api/v1/audio-quality"
_QUALITY_FILTERS = ("all", "A", "B", "C", "D", "unchecked", "issues", "failed")

_USAGE = (
    "usage: opendj track key-segments <stable_id> [--state-db PATH] [--json]\n"
    "       opendj track grid-flags [flagged|suspect|variable_tempo|unknown|ok|all]\n"
    "                               [include-dismissed] [all-tracks] [limit N]\n"
    "       opendj track grid-scan [all-tracks] [no-wait]\n"
    "       opendj track grid-flag <stable_id> dismiss|restore\n"
    "       opendj track source-flags [all|length_mismatch|stems_other_recording|\n"
    "                                 hardlinked_write | <stable_id> | scan]\n"
    "       opendj track grid-edit <stable_id> [show | anchors | nudge MS | shift BEATS |\n"
    "                              set MS BEATS | reset | undo | anchor T_S BPM |\n"
    "                              unanchor T_S] [rekordbox|own]\n"
    "       opendj track bpm-pick <stable_id> own|mik|clear\n"
    "       opendj track grid-fit [findings|wrong_bpm|off_beat|shifted|no_verdict|fit|unknown|all] [limit N]\n"
    "       opendj track grid-fit <stable_id> [force]\n"
    "       opendj track grid-fit-scan [no-wait]\n"
    "       opendj track quality [all|A|B|C|D|unchecked|issues|failed] [limit N]\n"
    "       opendj track quality-scan [limit N] | quality-scan status | quality-scan stop\n"
    "       opendj track quality-of <stable_id>\n"
    "       opendj track auto-gain <stable_id> [set <dB>|clear]\n"
    "       opendj track transition-in <stable_id> [set <ms>|clear]\n"
    "       opendj track tempo <stable_id>\n"
    "       opendj track played-length <stable_id>...\n"
    "       opendj track list-totals <stable_id>...\n"
    "       opendj track comment <stable_id> set <text> | clear\n"
    "       opendj track hide-in-tracklists <stable_id> on|off\n"
    "       opendj track related <stable_id> [limit N] [bpm-percent N]\n"
    "                            [key|genre|color|rating|mytag|artist 0|1]..."
)


def run(
    rest: Sequence[str],
    *,
    as_json: bool,
    state_db: Path | None = None,
) -> int:
    if not rest:
        print(_USAGE, file=sys.stderr)
        return 1
    verb, *args = rest
    if verb == KEY_SEGMENTS_VERB:
        return _key_segments(args, as_json=as_json, default_state_db=state_db)
    handler = _API_VERBS.get(verb)
    if handler is not None:
        return handler(args)
    print(f"unknown track verb: {verb!r}\n{_USAGE}", file=sys.stderr)
    return 1


def _usage_error() -> int:
    print(_USAGE, file=sys.stderr)
    return 1


def _grid_flags(args: Sequence[str]) -> int:
    """`GET /api/v1/beatgrid-flags`: flagged tracks with their numbers.

    Words, not `--options`: the top-level parser refuses options it does not
    know before this module sees them.
    """
    query: list[str] = []
    position = 0
    while position < len(args):
        token = args[position]
        if token in _GRID_CLASSES:
            query.append(f"grid_class={token}")
        elif token == "include-dismissed":
            query.append("include_dismissed=true")
        elif token == "all-tracks":
            query.append("availability=all")
        elif token == "limit":
            if position + 1 >= len(args) or not args[position + 1].isdigit():
                return _usage_error()
            query.append(f"limit={args[position + 1]}")
            position += 1
        else:
            return _usage_error()
        position += 1
    suffix = f"?{'&'.join(query)}" if query else ""
    return api_cli.run(["GET", f"{_GRID_FLAGS_PATH}{suffix}"], as_json=True)


def _source_flags(args: Sequence[str]) -> int:
    """SRCFLAG-01..03: `GET /api/v1/source-flags[?code=]`, one track, or `scan`."""
    if not args:
        return api_cli.run(["GET", _SOURCE_FLAGS_PATH], as_json=True)
    if len(args) != 1:
        return _usage_error()
    token = args[0]
    if token == "scan":
        return api_cli.run(["POST", f"{_SOURCE_FLAGS_PATH}/scan"], as_json=True)
    elif token in _SOURCE_FLAG_CODES:
        return api_cli.run(["GET", f"{_SOURCE_FLAGS_PATH}?code={token}"], as_json=True)
    return api_cli.run(["GET", f"{_SOURCE_FLAGS_PATH}/{token}"], as_json=True)


def _grid_scan(args: Sequence[str]) -> int:
    """`POST /api/v1/beatgrid-flags/scan`: bring stored verdicts up to date."""
    if any(token not in ("all-tracks", "no-wait") for token in args):
        return _usage_error()
    scope = "all" if "all-tracks" in args else "present"
    wait = "false" if "no-wait" in args else "true"
    return api_cli.run(["POST", f"{_GRID_FLAGS_PATH}/scan?scope={scope}&wait={wait}"], as_json=True)


def _grid_flag(args: Sequence[str]) -> int:
    """`PUT /api/v1/beatgrid-flags/{id}/dismissed`: hide or restore one flag."""
    if len(args) != 2 or args[1] not in ("dismiss", "restore"):
        return _usage_error()
    body = json.dumps({"dismissed": args[1] == "dismiss"})
    return api_cli.run(["PUT", f"{_GRID_FLAGS_PATH}/{args[0]}/dismissed", "--json", body], as_json=True)


def _number(token: str, *, integer: bool) -> float | None:
    try:
        return int(token) if integer else float(token)
    except ValueError:
        return None


def _grid_edit_write(action: str, values: Sequence[str]) -> tuple[str, str, dict[str, object]] | None:
    """(method, path suffix, body fields) for a grid-edit write, or None when malformed."""
    if action in ("nudge", "shift") and len(values) == 1:
        step = _number(values[0], integer=action == "shift")
        return None if step is None else ("POST", "/nudge", {"offset_ms" if action == "nudge" else "beats": step})
    if action == "set" and len(values) == 2:
        offset_ms = _number(values[0], integer=False)
        beat_shift = _number(values[1], integer=True)
        if offset_ms is None or beat_shift is None:
            return None
        return ("PUT", "", {"offset_ms": offset_ms, "beat_shift": beat_shift})
    if action == "anchor" and len(values) == 2:
        at_s = _number(values[0], integer=False)
        bpm = _number(values[1], integer=False)
        if at_s is None or bpm is None:
            return None
        return ("POST", "/anchors", {"at_s": at_s, "bpm": bpm})
    return None


def _grid_edit(args: Sequence[str]) -> int:
    """`/api/v1/tracks/{id}/grid-edit`: what the deck's two grid buttons do.

    `nudge MS` is grid adjust (ms, later when positive), `shift BEATS` is grid
    shift (whole beats, later when positive), `set MS BEATS` writes absolute
    values, `reset` returns to the measured grid, `undo` steps back one stored
    edit (GRIDEDIT-10215). `anchor T_S BPM` changes tempo from the grid line
    nearest T_S on, `unanchor T_S` removes that anchor and `anchors` lists them
    (GRIDEDIT-10220). A trailing `rekordbox` or
    `own` picks the grid; omitted, it is the one the deck is drawing.
    """
    if not args:
        return _usage_error()
    stable_id, *rest = args
    source = rest.pop() if rest and rest[-1] in _GRID_EDIT_SOURCES else None
    action, *values = rest or ["show"]
    path = f"/api/v1/tracks/{stable_id}/grid-edit"
    query = "" if source is None else f"?source={source}"
    if action in ("show", "reset", "anchors") and not values:
        method = "DELETE" if action == "reset" else "GET"
        suffix = "/anchors" if action == "anchors" else ""
        return api_cli.run([method, f"{path}{suffix}{query}"], as_json=True)
    if action == "undo" and not values:
        return api_cli.run(["POST", f"{path}/undo{query}"], as_json=True)
    if action == "unanchor" and len(values) == 1 and _number(values[0], integer=False) is not None:
        return api_cli.run(["DELETE", f"{path}/anchors/{values[0]}{query}"], as_json=True)
    write = _grid_edit_write(action, values)
    if write is None:
        return _usage_error()
    method, suffix, fields = write
    body = fields if source is None else {"source": source, **fields}
    return api_cli.run([method, f"{path}{suffix}", "--json", json.dumps(body)], as_json=True)


def _bpm_pick(args: Sequence[str]) -> int:
    """Serve our tempo or Mixed In Key's where they disagree (`clear` shows the dispute again)."""
    if len(args) != 2 or args[1] not in ("own", "mik", "clear"):
        return _usage_error()
    stable_id, pick = args
    path = f"/api/v1/tracks/{stable_id}/bpm-pick"
    if pick == "clear":
        return api_cli.run(["DELETE", path], as_json=True)
    return api_cli.run(["PUT", path, "--json", json.dumps({"source": pick})], as_json=True)


def _is_stable_id(token: str) -> bool:
    return len(token) == 40 and all(c in "0123456789abcdef" for c in token)


def _grid_fit(args: Sequence[str]) -> int:
    """One track: `GET /api/v1/beatgrid-flags/{id}/audio-fit` (judged now if
    stale). Otherwise `GET /api/v1/beatgrid-flags/audio-fit` by verdict."""
    if args and _is_stable_id(args[0]):
        if any(token != "force" for token in args[1:]):
            return _usage_error()
        suffix = "?force=true" if "force" in args[1:] else ""
        return api_cli.run(["GET", f"{_GRID_FLAGS_PATH}/{args[0]}/audio-fit{suffix}"], as_json=True)
    query: list[str] = []
    position = 0
    while position < len(args):
        token = args[position]
        if token in _FIT_FILTERS:
            query.append(f"verdict={token}")
        elif token == "limit":
            if position + 1 >= len(args) or not args[position + 1].isdigit():
                return _usage_error()
            query.append(f"limit={args[position + 1]}")
            position += 1
        else:
            return _usage_error()
        position += 1
    suffix = f"?{'&'.join(query)}" if query else ""
    return api_cli.run(["GET", f"{_GRID_FLAGS_PATH}/audio-fit{suffix}"], as_json=True)


def _grid_fit_scan(args: Sequence[str]) -> int:
    """`POST /api/v1/beatgrid-flags/audio-fit/scan`: judge every present track."""
    if any(token != "no-wait" for token in args):
        return _usage_error()
    wait = "false" if "no-wait" in args else "true"
    return api_cli.run(["POST", f"{_GRID_FLAGS_PATH}/audio-fit/scan?wait={wait}"], as_json=True)


def _number_words(args: Sequence[str], flags: Sequence[str], numbers: Sequence[str]) -> dict[str, str] | None:
    """`["no-wait", "limit", "5"]` -> `{"no-wait": "", "limit": "5"}`, or None
    when a word is unknown or a number is missing."""
    parsed: dict[str, str] = {}
    position = 0
    while position < len(args):
        token = args[position]
        if token in flags:
            parsed[token] = ""
        elif token in numbers:
            if position + 1 >= len(args) or not args[position + 1].isdigit():
                return None
            parsed[token] = args[position + 1]
            position += 1
        else:
            return None
        position += 1
    return parsed


def _quality(args: Sequence[str]) -> int:
    """`GET /api/v1/audio-quality`: letters, counts and issues."""
    parsed = _number_words(args, _QUALITY_FILTERS, ("limit",))
    if parsed is None or sum(key in _QUALITY_FILTERS for key in parsed) > 1:
        return _usage_error()
    query = [f"letter={key}" for key in parsed if key in _QUALITY_FILTERS]
    if "limit" in parsed:
        query.append(f"limit={parsed['limit']}")
    suffix = f"?{'&'.join(query)}" if query else ""
    return api_cli.run(["GET", f"{_QUALITY_PATH}{suffix}"], as_json=True)


def _quality_scan(args: Sequence[str]) -> int:
    """Start the background scan and return its status at once; `status` and
    `stop` read or end it. Never a blocking request: a library scan runs for
    minutes to hours, far past the CLI's HTTP timeout."""
    if list(args) == ["stop"]:
        return api_cli.run(["POST", f"{_QUALITY_PATH}/scan/stop"], as_json=True)
    if list(args) == ["status"]:
        return api_cli.run(["GET", f"{_QUALITY_PATH}/scan"], as_json=True)
    parsed = _number_words(args, (), ("limit",))
    if parsed is None:
        return _usage_error()
    query = "?limit=" + parsed["limit"] if "limit" in parsed else ""
    return api_cli.run(["POST", f"{_QUALITY_PATH}/scan{query}"], as_json=True)


def _quality_of(args: Sequence[str]) -> int:
    """`GET /api/v1/audio-quality/{id}`: one track's letter, hover and facts."""
    if len(args) != 1:
        return _usage_error()
    return api_cli.run(["GET", f"{_QUALITY_PATH}/{args[0]}"], as_json=True)


def _auto_gain(args: Sequence[str]) -> int:
    """`GET` one track's auto gain, or `PUT` the user's own offset (set/clear)."""
    if len(args) == 1:
        return api_cli.run(["GET", f"{_AUTO_GAIN_PATH}/{args[0]}"], as_json=True)
    if len(args) == 2 and args[1] == "clear":
        offset: float | None = None
    elif len(args) == 3 and args[1] == "set":
        try:
            offset = float(args[2])
        except ValueError:
            return _usage_error()
    else:
        return _usage_error()
    body = json.dumps({"offset_db": offset})
    return api_cli.run(["PUT", f"{_AUTO_GAIN_PATH}/{args[0]}/user-offset", "--json", body], as_json=True)


def _transition_in(args: Sequence[str]) -> int:
    """`GET` one track's transition-in cue, `PUT` it at a time (snapped), or `DELETE` it."""
    path = f"/api/v1/tracks/{args[0]}/transition-in" if args else ""
    if len(args) == 1:
        return api_cli.run(["GET", path], as_json=True)
    if len(args) == 2 and args[1] == "clear":
        return api_cli.run(["DELETE", path], as_json=True)
    if len(args) == 3 and args[1] == "set" and args[2].isdigit():
        return api_cli.run(["PUT", path, "--json", json.dumps({"in_ms": int(args[2])})], as_json=True)
    return _usage_error()


def _played_length(args: Sequence[str]) -> int:
    """`POST /api/v1/tracks/played-length`: rough mixed length of these tracks (LIBUX played length)."""
    if not args:
        return _usage_error()
    body = json.dumps({"stable_ids": list(args)})
    return api_cli.run(["POST", "/api/v1/tracks/played-length", "--json", body], as_json=True)


def _list_totals(args: Sequence[str]) -> int:
    """`POST /api/v1/tracks/list-totals`: track count and summed duration (LIBUX list footer)."""
    if not args:
        return _usage_error()
    body = json.dumps({"stable_ids": list(args)})
    return api_cli.run(["POST", "/api/v1/tracks/list-totals", "--json", body], as_json=True)


def _tempo(args: Sequence[str]) -> int:
    """`GET /api/v1/tracks/{id}/tempo-stability`: verdict and tempo segments."""
    if len(args) != 1:
        return _usage_error()
    return api_cli.run(["GET", f"/api/v1/tracks/{args[0]}/tempo-stability"], as_json=True)


def _related(args: Sequence[str]) -> int:
    """`GET /api/v1/tracks/{id}/related`: only the flags given are sent; the rest use stored prefs."""
    if not args:
        return _usage_error()
    stable_id, rest = args[0], list(args[1:])
    if len(rest) % 2:
        return _usage_error()
    query: list[str] = []
    for flag, value in zip(rest[::2], rest[1::2], strict=True):
        name = flag.replace("-", "_")
        if (name in ("limit", "bpm_percent") and value.isdigit()) or (name in _RELATED_FLAGS and value in ("0", "1")):
            query.append(f"{name}={value}")
        else:
            return _usage_error()
    qs = f"?{'&'.join(query)}" if query else ""
    return api_cli.run(["GET", f"/api/v1/tracks/{stable_id}/related{qs}"], as_json=True)


def _tracklist_hidden(args: Sequence[str]) -> int:
    if len(args) != 2 or args[1] not in ("on", "off"):
        return _usage_error()
    hidden = args[1] == "on"
    try:
        base_url = api_cli.resolve_backend_base_url().base_url
    except EngineNotRunning as error:
        return _fail(True, "engine_not_running", str(error), EXIT_NO_ENGINE)
    try:
        with engine_client(base_url) as client:
            result = set_tracklist_hidden(client, args[0], hidden)
    except TrackCommentError as error:
        return _fail(True, error.code, error.message, EXIT_FAILED)
    except httpx.TransportError as error:
        return _fail(True, "engine_unreachable", f"{base_url}: {error}", EXIT_NO_ENGINE)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _comment(args: Sequence[str]) -> int:
    """`PATCH /api/v1/tracks/{id}` with `{"comments": ...}`: what the Comment cell editor saves."""
    if len(args) == 2 and args[1] == "clear":
        comment = ""
    elif len(args) == 3 and args[1] == "set":
        comment = args[2]
    else:
        return _usage_error()
    try:
        base_url = api_cli.resolve_backend_base_url().base_url
    except EngineNotRunning as error:
        return _fail(True, "engine_not_running", str(error), EXIT_NO_ENGINE)
    try:
        with engine_client(base_url) as client:
            result = set_track_comment(client, args[0], comment)
    except TrackCommentError as error:
        return _fail(True, error.code, error.message, EXIT_FAILED)
    except httpx.TransportError as error:
        return _fail(True, "engine_unreachable", f"{base_url}: {error}", EXIT_NO_ENGINE)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


_RBX_SOURCE_REASON = "key lane source is rekordbox, not own"

#: ``MDT_DATA_DIR`` is the repo-wide explicit override (apps/adapters/
#: rekordbox/config.py, apps/shared/platform_paths.py): set it and every data
#: path, including this one, follows it. Unset, ``config.STATE_DB`` falls
#: back to a path relative to wherever ``apps/shared/platform_paths.py`` sits
#: on disk -- the repo root in a checkout, but ``payload/app`` inside the
#: installed bundle, which is issue #3037. Unset means "no explicit dev
#: override", so the CLI falls back to the INSTALLED app's own data dir
#: instead: the directory the running engine's lock file lives in
#: (apps.engine_core.origin.lock_path), which is that engine's ``--data-dir``
#: by construction (apps.engine_core.config.EngineConfig.lock_path), and
#: honors the same ``OPENDJ_LIVE_LOCK_PATH`` override every other opendj_cli
#: command already reads. The CLI and a sandboxed engine can therefore never
#: disagree about which data dir is meant, and this never depends on where
#: the CLI's own code happens to be installed.
_DATA_DIR_ENV = "MDT_DATA_DIR"


def _resolve_state_db(state_db: Path | None) -> Path:
    """The concrete state DB path this invocation reads.

    ``--state-db`` (explicit) wins outright. Otherwise ``MDT_DATA_DIR`` wins
    (explicit dev/test override, matching every other reader of that env
    var). Otherwise the installed app's own data dir, derived from the
    engine lock file's location rather than this file's own path on disk --
    see the module docstring and the constant above.
    """
    if state_db is not None:
        return state_db
    data_dir_override = os.environ.get(_DATA_DIR_ENV, "").strip()
    if data_dir_override:
        return Path(data_dir_override) / "state" / "state.db"
    from apps.engine_core.origin import lock_path

    return lock_path().parent / "state" / "state.db"


def _track_exists(stable_id: str, state_db: Path) -> bool:
    conn = state_conn_ro(state_db)
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _not_found(stable_id: str, *, as_json: bool) -> int:
    return _fail(as_json, "not_found", f"track not found: {stable_id}", EXIT_FAILED)


def _state_db_missing(state_db: Path, *, as_json: bool) -> int:
    """A resolved-but-absent state DB: distinct from "track not found".

    Whether ``state_db`` came from ``--state-db`` or from the installed-app
    default, this invocation always names one concrete path, so its absence
    is always this error, never swallowed into "track not found" (issue
    #3037 defect 2).
    """
    return _fail(as_json, "state_db_not_found", f"state DB not found: {state_db}", EXIT_FAILED)


def _key_segments(
    args: Sequence[str],
    *,
    as_json: bool,
    default_state_db: Path | None = None,
) -> int:
    stable_id: str | None = None
    state_db = default_state_db
    position = 0
    while position < len(args):
        token = args[position]
        if token == "--state-db":
            if position + 1 >= len(args):
                print(_USAGE, file=sys.stderr)
                return 1
            state_db = Path(args[position + 1])
            position += 2
            continue
        if stable_id is not None:
            print(_USAGE, file=sys.stderr)
            return 1
        stable_id = token
        position += 1
    if stable_id is None:
        print(_USAGE, file=sys.stderr)
        return 1
    resolved_db = _resolve_state_db(state_db)
    if not resolved_db.exists():
        return _state_db_missing(resolved_db, as_json=as_json)
    if not _track_exists(stable_id, resolved_db):
        return _not_found(stable_id, as_json=as_json)
    # Lazy import: this CLI's other heads never touch the analysis package,
    # and importing it eagerly would pull librosa/numpy into every deck
    # command's startup path for a head most invocations never take.
    from apps.webui.server.rb_vendor_pkg.own_key_overlay import apply_own_key_segments

    overlay = apply_own_key_segments({}, stable_id, state_db_path=resolved_db)
    block = overlay.get("key_segments")
    if block is None:
        block = {
            "status": "missing",
            "reason": _RBX_SOURCE_REASON,
            "segments": [],
        }
    if as_json:
        print(json.dumps(block, indent=2, sort_keys=True))
        return 0
    print(f"status: {block['status']}")
    if block["reason"]:
        print(f"reason: {block['reason']}")
    for segment in block["segments"]:
        print(
            f"  bar {segment['start_bar']}-{segment['end_bar']}: "
            f"{segment['key_camelot']} ({segment['key_openkey']}) "
            f"confidence={segment['confidence']:.2f}"
        )
    return 0


__all__ = [
    "AUTO_GAIN_VERB",
    "GRID_EDIT_VERB",
    "GRID_FIT_SCAN_VERB",
    "GRID_FIT_VERB",
    "GRID_FLAGS_VERB",
    "GRID_FLAG_VERB",
    "GRID_SCAN_VERB",
    "KEY_SEGMENTS_VERB",
    "SOURCE_FLAGS_VERB",
    "TEMPO_VERB",
    "TRANSITION_IN_VERB",
    "run",
    "run_history",
]


#: Verbs that only wrap an engine route. `run` looks them up here, so a new
#: verb is one line rather than another branch.
_API_VERBS: dict[str, Callable[[Sequence[str]], int]] = {
    GRID_FLAGS_VERB: _grid_flags,
    SOURCE_FLAGS_VERB: _source_flags,
    GRID_SCAN_VERB: _grid_scan,
    GRID_FLAG_VERB: _grid_flag,
    GRID_FIT_VERB: _grid_fit,
    GRID_FIT_SCAN_VERB: _grid_fit_scan,
    QUALITY_VERB: _quality,
    QUALITY_SCAN_VERB: _quality_scan,
    QUALITY_OF_VERB: _quality_of,
    AUTO_GAIN_VERB: _auto_gain,
    GRID_EDIT_VERB: _grid_edit,
    BPM_PICK_VERB: _bpm_pick,
    TRANSITION_IN_VERB: _transition_in,
    TEMPO_VERB: _tempo,
    "played-length": _played_length,
    "list-totals": _list_totals,
    COMMENT_VERB: _comment,
    TRACKLIST_HIDDEN_VERB: _tracklist_hidden,
    RELATED_VERB: _related,
}

_HISTORY_USAGE = (
    "usage: opendj history delete-play <stable_id> <play_id> [--json]\n"
    "       opendj history delete-day <YYYY-MM-DD> [IANA_ZONE] [--json]\n"
    "\n"
    "delete-play removes one Open DJ play for the track (the HTTP route is keyed by track).\n"
    "delete-day removes every play from local 06:00 that day to 06:00 the next.\n"
    "Optional IANA_ZONE sets the 06:00 boundary (default: server local). For an explicit\n"
    "tz query use: opendj api DELETE /api/v1/history/<date>?tz=Zone"
)


def _history_delete_play(args: Sequence[str], *, as_json: bool) -> int:
    if len(args) != 2:
        print(_HISTORY_USAGE, file=sys.stderr)
        return 1
    stable_id, play_id = args
    return api_cli.run(
        ["DELETE", f"/api/v1/tracks/{stable_id}/plays/{play_id}"],
        as_json=as_json,
    )


def _history_delete_day(args: Sequence[str], *, as_json: bool) -> int:
    if len(args) < 1 or len(args) > 2:
        print(_HISTORY_USAGE, file=sys.stderr)
        return 1
    day_str = args[0]
    tz = args[1] if len(args) == 2 else None
    try:
        from datetime import date

        day = date.fromisoformat(day_str)
    except ValueError:
        print(_HISTORY_USAGE, file=sys.stderr)
        return 1
    path = f"/api/v1/history/{day.isoformat()}"
    if tz is not None:
        from urllib.parse import quote

        path = f"{path}?tz={quote(tz, safe='')}"
    return api_cli.run(["DELETE", path], as_json=as_json)


def run_history(
    rest: Sequence[str],
    *,
    as_json: bool,
    state_db: Path | None = None,
) -> int:
    del state_db  # history verbs use the engine HTTP path like other api_cli wrappers
    if not rest:
        print(_HISTORY_USAGE, file=sys.stderr)
        return 1
    verb, *args = rest
    if verb == "delete-play":
        return _history_delete_play(args, as_json=as_json)
    if verb == "delete-day":
        return _history_delete_day(args, as_json=as_json)
    print(f"unknown history verb: {verb!r}\n{_HISTORY_USAGE}", file=sys.stderr)
    return 1
