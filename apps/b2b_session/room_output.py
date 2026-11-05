"""B2BROOM-01..03: which laptop plays to the room, pure half (no FastAPI, no I/O but the store).

In a B2B set each laptop plays its own audio. Only the one wired to the PA should feed the
room: two laptops on two speaker systems give double audio, smeared by the latency between
them, and two laptops with speakers off give a silent room.

- **Speakers switch** (B2BROOM-01): this laptop's choice, stored in
  ``<state dir>/b2b-speakers.json`` next to ``state.db`` (written atomically, so an "off"
  survives a restart; losing it would bring the room audio back unannounced). The page reads
  it and zeroes ONLY the room path, so the headphone CUE/MIX still hears master.
- **Room output on the wire** (B2BROOM-02): the page publishes what it applied as the
  ui-mirror's ``room_output`` (``{speakers_on, room_path}``); ``shape_room_output`` turns it
  into the ``room`` field of ``GET /api/v1/b2b/now-playing``, which the paired laptop reads
  through the B2B door. An older page yields ``None``, never a default.
- **Room verdict** (B2BROOM-03): ``room_verdict`` combines both sides into
  ``this_laptop | their_laptop | both | none | unknown`` plus a warning on ``both`` and
  ``none``. ``tests/fixtures/b2b-room-verdicts.json`` is the shared table the page's
  TypeScript twin (``room-output.ts``) is tested against, so the two cannot drift.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

SPEAKERS_FILENAME = "b2b-speakers.json"
#: With no stored choice the laptop plays to its speakers, as it always did before the switch.
SPEAKERS_ON_WHEN_UNSET = True

RoomPath = Literal["room_only", "whole_output"]
_ROOM_PATHS: frozenset[str] = frozenset({"room_only", "whole_output"})
RoomVerdict = Literal["this_laptop", "their_laptop", "both", "none", "unknown"]
#: The peer side when no peer is configured at all (a solo laptop), distinct from "not reported".
PEER_ABSENT = "absent"

WARNINGS: dict[str, dict[str, str]] = {
    "both": {
        "code": "double_audio",
        "text": "Both laptops are playing to the room: double audio, smeared by the network latency "
        "between them. Turn Speakers off on the laptop that is not wired to the PA.",
    },
    "none": {
        "code": "silent_room",
        "text": "Neither laptop is playing to the room: the room is silent. Turn Speakers on on the "
        "laptop wired to the PA.",
    },
}


# ---------------------------------------------------------------- store


def read_speakers(path: Path) -> tuple[bool, str]:
    """``(speakers_on, source)``: the stored choice (``stored``), else ``SPEAKERS_ON_WHEN_UNSET``
    (``unset``). A corrupt file raises rather than guessing a side."""
    if not path.exists():
        return SPEAKERS_ON_WHEN_UNSET, "unset"
    data = json.loads(path.read_text(encoding="utf-8"))
    value = data.get("speakers_on") if isinstance(data, dict) else None
    if not isinstance(value, bool):
        raise TypeError(f"{path} is not a speakers record: {data!r}")
    return value, "stored"


def write_speakers(path: Path, speakers_on: bool) -> None:
    if not isinstance(speakers_on, bool):
        raise TypeError("speakers_on must be a bool")
    record = {"speakers_on": speakers_on, "saved_at": datetime.now(UTC).isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def clear_speakers(path: Path) -> bool:
    """B2BROOM-05: forget the stored choice, so the laptop is back on ``SPEAKERS_ON_WHEN_UNSET``
    (``source: unset``). True when a stored choice was removed, False when there was none."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


# ---------------------------------------------------------------- wire shape


def shape_room_output(room_output: Any) -> dict[str, Any] | None:
    """The mirror's ``room_output`` as ``{speakers_on, room_path}``; ``None`` when the page does not
    publish one or publishes a malformed one (an older build): never a default side."""
    if not isinstance(room_output, dict):
        return None
    speakers_on, room_path = room_output.get("speakers_on"), room_output.get("room_path")
    if not isinstance(speakers_on, bool) or room_path not in _ROOM_PATHS:
        return None
    return {"speakers_on": speakers_on, "room_path": room_path}


# ---------------------------------------------------------------- verdict


def room_verdict(local: bool | None, peer: bool | str | None) -> dict[str, Any]:
    """Who plays to the room. ``local``/``peer`` are speakers-on flags; ``None`` is not known
    (no page, an older peer, an unreachable peer); ``peer == PEER_ABSENT`` is no peer at all.
    Unknown on either side is ``unknown``: a guess here would hide double audio or a silent room."""
    if peer == PEER_ABSENT:
        peer = False
    if local is None or peer is None:
        room: RoomVerdict = "unknown"
    elif local and peer:
        room = "both"
    elif local:
        room = "this_laptop"
    elif peer:
        room = "their_laptop"
    else:
        room = "none"
    return {"room": room, "warning": WARNINGS.get(room)}
