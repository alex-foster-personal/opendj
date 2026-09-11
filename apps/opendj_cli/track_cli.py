"""``opendj track <verb>``: the CLI's read-only track-analysis surface.

Unlike every other ``opendj`` head, ``track`` never touches the AGENT-03
engine bus: it reads straight from the state store, the same read `/anlz`
does, so an agent (or a human) can inspect one track's analysis without a
running deck engine.

``opendj track key-segments <stable_id>`` is nav1-key-record's own scope-lock
line (specs/native-analysis-v1-lanes/nav1-key-record.md item 3): it must
print the SAME `key_segments` block the `/anlz` payload carries, reusing the
identical overlay function rather than a second read of the record.

-Claude
"""
from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

KEY_SEGMENTS_VERB = "key-segments"

_USAGE = (
    "usage: opendj track key-segments <stable_id> [--state-db PATH] [--json]"
)


def run(rest: Sequence[str], *, as_json: bool) -> int:
    if not rest:
        print(_USAGE, file=sys.stderr)
        return 1
    verb, *args = rest
    if verb == KEY_SEGMENTS_VERB:
        return _key_segments(args, as_json=as_json)
    print(f"unknown track verb: {verb!r}\n{_USAGE}", file=sys.stderr)
    return 1


def _key_segments(args: Sequence[str], *, as_json: bool) -> int:
    stable_id: str | None = None
    state_db: Path | None = None
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
    # Lazy import: this CLI's other heads never touch the analysis package,
    # and importing it eagerly would pull librosa/numpy into every deck
    # command's startup path for a head most invocations never take.
    from apps.webui.server.rb_vendor_pkg.own_key_overlay import apply_own_key_segments

    block = apply_own_key_segments(
        {}, stable_id, state_db_path=state_db
    )["key_segments"]
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
