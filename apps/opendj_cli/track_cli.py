"""``opendj track <verb>``: the CLI's track-analysis surface.

Unlike every other ``opendj`` head, ``track`` never touches the AGENT-03
engine bus. ``key-segments`` reads straight from the state store, the same
read `/anlz` does, so an agent (or a human) can inspect one track's analysis
without a running deck engine. The ``grid-*`` verbs (GRIDFLAG-03) are the
exception to "no engine": they are the CLI twin of ``/api/v1/beatgrid-flags``
and call the running engine over HTTP, because the scan and the dismissal
write state the engine owns.

``opendj track key-segments <stable_id>`` is nav1-key-record's own scope-lock
line (specs/native-analysis-v1-lanes/nav1-key-record.md item 3): it must
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
from collections.abc import Sequence
from pathlib import Path

from apps.opendj_cli import EXIT_FAILED, api_cli
from apps.webui.server.rb_vendor_pkg.own_lane_store import state_conn_ro

KEY_SEGMENTS_VERB = "key-segments"
#: GRIDFLAG-03: the CLI twin of `/api/v1/beatgrid-flags` (the Err column's
#: beatgrid flag). These three talk to the running engine over HTTP, because
#: the scan and the dismissal are the engine's to do; they print its JSON.
GRID_FLAGS_VERB = "grid-flags"
GRID_SCAN_VERB = "grid-scan"
GRID_FLAG_VERB = "grid-flag"
_GRID_FLAGS_PATH = "/api/v1/beatgrid-flags"
_GRID_CLASSES = ("flagged", "suspect", "variable_tempo", "unknown", "ok", "all")

_USAGE = (
    "usage: opendj track key-segments <stable_id> [--state-db PATH] [--json]\n"
    "       opendj track grid-flags [flagged|suspect|variable_tempo|unknown|ok|all]\n"
    "                               [include-dismissed] [all-tracks] [limit N]\n"
    "       opendj track grid-scan [all-tracks] [no-wait]\n"
    "       opendj track grid-flag <stable_id> dismiss|restore"
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
    if verb == GRID_FLAGS_VERB:
        return _grid_flags(args)
    if verb == GRID_SCAN_VERB:
        return _grid_scan(args)
    if verb == GRID_FLAG_VERB:
        return _grid_flag(args)
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


def _grid_scan(args: Sequence[str]) -> int:
    """`POST /api/v1/beatgrid-flags/scan`: bring stored verdicts up to date."""
    if any(token not in ("all-tracks", "no-wait") for token in args):
        return _usage_error()
    scope = "all" if "all-tracks" in args else "present"
    wait = "false" if "no-wait" in args else "true"
    return api_cli.run(
        ["POST", f"{_GRID_FLAGS_PATH}/scan?scope={scope}&wait={wait}"], as_json=True
    )


def _grid_flag(args: Sequence[str]) -> int:
    """`PUT /api/v1/beatgrid-flags/{id}/dismissed`: hide or restore one flag."""
    if len(args) != 2 or args[1] not in ("dismiss", "restore"):
        return _usage_error()
    body = json.dumps({"dismissed": args[1] == "dismiss"})
    return api_cli.run(
        ["PUT", f"{_GRID_FLAGS_PATH}/{args[0]}/dismissed", "--json", body], as_json=True
    )


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
    message = f"track not found: {stable_id}"
    if as_json:
        print(
            json.dumps(
                {
                    "error": {"code": "not_found", "message": message},
                    "exit_code": EXIT_FAILED,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"opendj: {message}", file=sys.stderr)
    return EXIT_FAILED


def _state_db_missing(state_db: Path, *, as_json: bool) -> int:
    """A resolved-but-absent state DB: distinct from "track not found".

    Whether ``state_db`` came from ``--state-db`` or from the installed-app
    default, this invocation always names one concrete path, so its absence
    is always this error, never swallowed into "track not found" (issue
    #3037 defect 2).
    """
    message = f"state DB not found: {state_db}"
    if as_json:
        print(
            json.dumps(
                {
                    "error": {"code": "state_db_not_found", "message": message},
                    "exit_code": EXIT_FAILED,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"opendj: {message}", file=sys.stderr)
    return EXIT_FAILED


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
    "GRID_FLAGS_VERB",
    "GRID_FLAG_VERB",
    "GRID_SCAN_VERB",
    "KEY_SEGMENTS_VERB",
    "run",
]
