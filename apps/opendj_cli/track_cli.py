"""``opendj track <verb>``: the CLI's read-only track-analysis surface.

Unlike every other ``opendj`` head, ``track`` never touches the AGENT-03
engine bus: it reads straight from the state store, the same read `/anlz`
does, so an agent (or a human) can inspect one track's analysis without a
running deck engine.

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

from apps.opendj_cli import EXIT_FAILED
from apps.webui.server.rb_vendor_pkg.own_lane_store import state_conn_ro

KEY_SEGMENTS_VERB = "key-segments"

_USAGE = (
    "usage: opendj track key-segments <stable_id> [--state-db PATH] [--json]"
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
    print(f"unknown track verb: {verb!r}\n{_USAGE}", file=sys.stderr)
    return 1


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


__all__ = ["KEY_SEGMENTS_VERB", "run"]
