#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "python-rtmidi>=1.5,<2",
# ]
# ///
"""Live MIDI capture probe for DDJ-FLX4 map adjudication (onboarding tier T3).

The runtime map at ``apps/webui/frontend/src/lib/rb/midi/maps/ddj-flx4.ts`` and
the expected map beside this file are THEORY-FIRST: both were derived from the
vendor MIDI message list PDF, and no wire in either has been confirmed against
the hardware. This CLI is the instrument that confirms them. It is the
agent-native equivalent of the in-app learn wizard in
``specs/controller-onboarding.md`` section 3.4.

Subcommands::

    ports    list CoreMIDI input ports
    sniff    stream decoded inbound messages with expected-map reverse lookup
    capture  walk the expected controls one at a time and record the wire bytes
    diff     compare a capture file to the expected map; exit 1 on any mismatch

Usage::

    uv run tools/controller-probe/capture.py ports
    uv run tools/controller-probe/capture.py sniff
    uv run tools/controller-probe/capture.py capture --out .tmp/flx4-deck1.json \
        --section deck --deck 1
    uv run tools/controller-probe/capture.py diff .tmp/flx4-deck1.json

File requirements (mini-PRD):

* R1 list input ports, fail loud when CoreMIDI reports none. ✔︎ ✅ 🎯
  - [if] no MIDI device is attached [then] ``ports`` exits nonzero naming the
    empty port list, rather than printing an empty table and exiting 0.
  - [if] one or more ports exist [then] each is printed with its index.
* R2 open exactly one port by substring, never guess. ✔︎ ✅ 🎯
  - [if] the needle matches zero ports [then] exit nonzero listing every
    candidate name.
  - [if] the needle matches more than one port [then] exit nonzero listing the
    matches, rather than silently taking the first.
* R3 decode and reverse-look-up every non-realtime inbound message. ✔︎ ✅ 🎯
  - [if] a clock byte (status >= 0xF8) arrives [then] it is dropped, so a
    controller sending clock cannot flood the log.
  - [if] a message's (status, code) is absent from the expected map [then] the
    line reads UNMAPPED rather than being attributed to a neighbor.
* R4 guided capture writes a crash-safe JSON record. ✔︎ ✅ 🎯
  - [if] the walk is quit at step k [then] the file on disk holds the k-1
    completed entries and exits 0.
  - [if] a 14-bit CC sends MSB and LSB [then] both (status, data1) pairs appear
    in the entry's observed list.
* R5 diff is a gate, not a report. ✔︎ ✅ 🎯
  - [if] any entry mismatches [then] the process exits 1.
  - [if] an observed pair matches no expected control at all [then] it is
    listed as EXTRA rather than dropped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import select
import sys
import termios
import time
import tty
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import rtmidi

# ----------------------------------------------------------------- constants

DEFAULT_MAP_PATH: Path = Path(__file__).resolve().parent / "flx4_expected_map.json"
DEFAULT_PORT_NEEDLE: str = "DDJ-FLX4"

# 0xF8 and above is system realtime (clock, start, stop, active sensing). The
# FLX4 emits clock continuously, which would drown every other line.
REALTIME_STATUS_FLOOR: int = 0xF8

# A continuous control is sampled for a window rather than a single message so
# a 14-bit control shows BOTH its MSB and LSB CC numbers in one step.
CC_WINDOW_S: float = 0.4
# A note step ends on its release; this bounds the wait when the release never
# lands (wrong control pressed, or a latching switch).
NOTE_RELEASE_TIMEOUT_S: float = 2.0
POLL_INTERVAL_S: float = 0.02

# One table rather than three sets plus a branch: the walk's whole keyboard
# contract is then readable in four lines and cannot drift between them.
KEY_ACTIONS: Mapping[str, str] = {
    "q": "quit",
    "Q": "quit",
    "b": "back",
    "B": "back",
    "s": "skip",
    "S": "skip",
    "\n": "skip",
    "\r": "skip",
}

VERDICT_MATCH: str = "match"
VERDICT_MISMATCH: str = "mismatch"
VERDICT_SKIPPED: str = "skipped"


# ---------------------------------------------------------------- data model


@dataclass(frozen=True)
class Decoded:
    """One decoded channel-voice message. ``channel`` is 1-based, as the map is."""

    status: int
    kind: str
    channel: int
    code: int
    value: int


@dataclass(frozen=True)
class ExpectedControl:
    """One row of the expected map."""

    id: str
    name: str
    fig: str
    section: str
    deck: int | None
    shift: bool
    ch: int
    kind: str
    code: int
    status: int


@dataclass(frozen=True)
class ExpectedMap:
    """The expected map plus its provenance and a (status, code) reverse index."""

    path: Path
    sha256: str
    device: str
    controls: tuple[ExpectedControl, ...]
    index: Mapping[tuple[int, int], ExpectedControl]


@dataclass(frozen=True)
class DiffRow:
    control_id: str
    expected_text: str
    observed_text: str
    verdict: str


@dataclass(frozen=True)
class DiffReport:
    rows: tuple[DiffRow, ...]
    match: int
    mismatch: int
    skipped: int
    not_attempted: int
    extras: tuple[tuple[int, int], ...]

    @property
    def summary_line(self) -> str:
        return (
            f"match {self.match} / mismatch {self.mismatch} / "
            f"skipped {self.skipped} / not-attempted {self.not_attempted}"
        )


# -------------------------------------------------------------- pure decoding


def is_realtime(status: int) -> bool:
    """System realtime bytes carry no channel and no data bytes."""
    return status >= REALTIME_STATUS_FLOOR


_KIND_BY_FAMILY: Mapping[int, str] = {
    0x80: "note_off",
    0x90: "note_on",
    0xA0: "aftertouch",
    0xB0: "cc",
    0xC0: "program",
    0xD0: "channel_pressure",
    0xE0: "pitchbend",
}


def message_kind(status: int) -> str:
    """Name the channel-voice family of ``status``, or ``other``."""
    return _KIND_BY_FAMILY.get(status & 0xF0, "other")


def decode_message(data: Sequence[int]) -> Decoded | None:
    """Decode a raw MIDI message, or return ``None`` when it carries no control.

    ``None`` means "deliberately not a control event": realtime/clock bytes, and
    anything shorter than status plus one data byte. Everything else decodes,
    including families the expected map does not use, so an unexpected family
    shows up as UNMAPPED rather than disappearing.
    """
    if not data:
        return None
    status = data[0]
    if is_realtime(status):
        return None
    if status < 0x80 or len(data) < 2:
        return None
    kind = message_kind(status)
    channel = (status & 0x0F) + 1
    code = data[1]
    value = data[2] if len(data) > 2 else 0
    return Decoded(status=status, kind=kind, channel=channel, code=code, value=value)


def is_note_release(decoded: Decoded) -> bool:
    """A release is either a note-off or the note-on-with-zero-velocity idiom."""
    return decoded.kind == "note_off" or (decoded.kind == "note_on" and decoded.value == 0)


# ------------------------------------------------------------ expected map io


def _control_from_dict(raw: Mapping[str, Any]) -> ExpectedControl:
    return ExpectedControl(
        id=raw["id"],
        name=raw["name"],
        fig=raw["fig"],
        section=raw["section"],
        deck=raw["deck"],
        shift=raw["shift"],
        ch=raw["ch"],
        kind=raw["kind"],
        code=raw["code"],
        status=raw["status"],
    )


def build_reverse_index(
    controls: Iterable[ExpectedControl],
) -> dict[tuple[int, int], ExpectedControl]:
    """Index controls by the exact wire pair the hardware will send."""
    index: dict[tuple[int, int], ExpectedControl] = {}
    for control in controls:
        index.setdefault((control.status, control.code), control)
    return index


def lookup_control(
    index: Mapping[tuple[int, int], ExpectedControl], status: int, code: int
) -> ExpectedControl | None:
    """Reverse-look-up one wire pair. Exact match only, never a near miss."""
    return index.get((status, code))


def is_accounted_for(
    index: Mapping[tuple[int, int], ExpectedControl], status: int, code: int
) -> bool:
    """Is this pair explained by the map, either directly or as a known release?

    The expected map lists the note-ON status for every button and no note-OFF
    rows at all, so a plain ``lookup_control`` marks every button RELEASE as an
    unknown wire. That would bury the genuinely unknown pairs under one EXTRA
    line per button pressed. A note-off is therefore accounted for when its
    own channel's note-on is mapped at the same code; anything else is not.
    """
    if lookup_control(index, status, code) is not None:
        return True
    if message_kind(status) == "note_off":
        note_on_status = (status & 0x0F) | 0x90
        return lookup_control(index, note_on_status, code) is not None
    return False


def load_expected_map(path: Path) -> ExpectedMap:
    """Load and index the expected map, failing loud on a missing or short file."""
    if not path.is_file():
        raise SystemExit(f"error: expected map not found at {path}")
    payload = path.read_bytes()
    doc = json.loads(payload.decode("utf-8"))
    controls = tuple(_control_from_dict(row) for row in doc["controls"])
    if not controls:
        raise SystemExit(f"error: expected map {path} holds zero controls")
    return ExpectedMap(
        path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        device=doc["device"],
        controls=controls,
        index=build_reverse_index(controls),
    )


def filter_controls(
    controls: Sequence[ExpectedControl],
    section: str | None = None,
    deck: int | None = None,
    no_shift: bool = False,
    ids: Sequence[str] | None = None,
) -> tuple[ExpectedControl, ...]:
    """Narrow the walk, preserving expected-map file order."""
    selected = list(controls)
    if section is not None:
        selected = [c for c in selected if c.section == section]
    if deck is not None:
        selected = [c for c in selected if c.deck == deck]
    if no_shift:
        selected = [c for c in selected if not c.shift]
    if ids:
        wanted = list(ids)
        known = {c.id for c in controls}
        missing = [i for i in wanted if i not in known]
        if missing:
            raise SystemExit(f"error: unknown control ids: {', '.join(missing)}")
        selected = [c for c in selected if c.id in set(wanted)]
    return tuple(selected)


# ------------------------------------------------------------------ verdicts


def compute_verdict(
    expected: Mapping[str, Any], observed: Sequence[Mapping[str, Any]], skipped: bool
) -> str:
    """Grade one capture entry.

    An explicit skip, and a step that gathered NO messages at all, are both
    ``skipped``: absence of evidence is not evidence of a wrong wire. Only a
    step that saw traffic and never saw the expected pair is a ``mismatch``.
    """
    if skipped or not observed:
        return VERDICT_SKIPPED
    wanted = (expected["status"], expected["code"])
    for message in observed:
        if (message["status"], message["code"]) == wanted:
            return VERDICT_MATCH
    return VERDICT_MISMATCH


def _format_expected(expected: Mapping[str, Any]) -> str:
    return (
        f"{expected['kind']} ch{expected['ch']} "
        f"status 0x{expected['status']:02X} code {expected['code']}"
    )


def _format_observed(observed: Sequence[Mapping[str, Any]]) -> str:
    if not observed:
        return "(nothing)"
    pairs: list[str] = []
    for message in observed:
        text = f"0x{message['status']:02X}/{message['code']}"
        if text not in pairs:
            pairs.append(text)
    return " ".join(pairs)


def build_diff_report(capture: Mapping[str, Any], expected_map: ExpectedMap) -> DiffReport:
    """Grade a whole capture file against the expected map.

    ``not_attempted`` counts expected-map controls with no entry in the capture
    at all. A filtered walk therefore reports a large not-attempted figure by
    construction; it is the count against the FULL map, not against the filter,
    because the capture file does not record which filter produced it.
    """
    rows: list[DiffRow] = []
    counts = {VERDICT_MATCH: 0, VERDICT_MISMATCH: 0, VERDICT_SKIPPED: 0}
    extras: list[tuple[int, int]] = []
    for entry in capture["entries"]:
        expected = entry["expected"]
        observed = entry["observed"]
        verdict = compute_verdict(expected, observed, entry["verdict"] == VERDICT_SKIPPED)
        counts[verdict] += 1
        rows.append(
            DiffRow(
                control_id=entry["id"],
                expected_text=_format_expected(expected),
                observed_text=_format_observed(observed),
                verdict=verdict,
            )
        )
        for message in observed:
            pair = (message["status"], message["code"])
            if not is_accounted_for(expected_map.index, *pair) and pair not in extras:
                extras.append(pair)
    attempted = {entry["id"] for entry in capture["entries"]}
    not_attempted = sum(1 for control in expected_map.controls if control.id not in attempted)
    return DiffReport(
        rows=tuple(rows),
        match=counts[VERDICT_MATCH],
        mismatch=counts[VERDICT_MISMATCH],
        skipped=counts[VERDICT_SKIPPED],
        not_attempted=not_attempted,
        extras=tuple(sorted(extras)),
    )


# ------------------------------------------------------------------ midi port


def _list_ports() -> tuple[rtmidi.MidiIn, list[str]]:
    midi_in = rtmidi.MidiIn()
    return midi_in, list(midi_in.get_ports())


def resolve_port(names: Sequence[str], needle: str) -> int:
    """Return the single index whose port name contains ``needle`` (case-insensitive)."""
    hits = [i for i, name in enumerate(names) if needle.lower() in name.lower()]
    if len(hits) == 1:
        return hits[0]
    listing = "\n".join(f"  [{i}] {name}" for i, name in enumerate(names)) or "  (none)"
    if not hits:
        raise SystemExit(f"error: no input port matching {needle!r}. Ports:\n{listing}")
    raise SystemExit(
        f"error: {len(hits)} input ports match {needle!r}; narrow it with --port. Ports:\n{listing}"
    )


def _open_port(needle: str) -> tuple[rtmidi.MidiIn, str]:
    midi_in, names = _list_ports()
    if not names:
        raise SystemExit("error: CoreMIDI reports zero input ports. Is the controller attached?")
    index = resolve_port(names, needle)
    midi_in.open_port(index)
    # Realtime bytes are filtered here as well as in decode_message, so the
    # queue never fills with clock between polls.
    midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
    return midi_in, names[index]


def _make_callback(events: queue.Queue[tuple[str, tuple[int, ...]]]) -> Callable[..., None]:
    def _on_message(event: tuple[Sequence[int], float], _data: Any = None) -> None:
        message, _delta = event
        events.put((_utc_now(), tuple(message)))

    return _on_message


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- stdin keys


@dataclass
class _KeyPoller:
    """Poll one keystroke without blocking the MIDI queue drain."""

    enabled: bool = True

    def poll(self, timeout: float) -> str | None:
        if not self.enabled:
            time.sleep(timeout)
            return None
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        raw = os.read(sys.stdin.fileno(), 1)
        if not raw:
            # EOF (piped stdin already drained). Stop selecting on it, or the
            # loop spins at 100% CPU.
            self.enabled = False
            return None
        return raw.decode("utf-8", "replace")


@contextmanager
def _cbreak_stdin() -> Any:
    """Single-keypress stdin on a tty; a no-op anywhere else."""
    if not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


# ------------------------------------------------------------- capture output


def _observed_record(stamp: str, data: Sequence[int], decoded: Decoded) -> dict[str, Any]:
    return {
        "t_utc": stamp,
        "bytes": list(data),
        "status": decoded.status,
        "ch": decoded.channel,
        "code": decoded.code,
        "value": decoded.value,
    }


def _write_capture(path: Path, doc: Mapping[str, Any]) -> None:
    """Write atomically, so a crash mid-walk cannot truncate the record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _prompt_for(control: ExpectedControl, position: int, total: int) -> str:
    deck = f"deck {control.deck}" if control.deck is not None else "no deck"
    shift = ", +SHIFT" if control.shift else ""
    return (
        f"[{position}/{total}] Press: {control.name} ({deck}{shift}) "
        f"expecting {control.kind} ch{control.ch} code {control.code}"
    )


def _drain(events: queue.Queue[tuple[str, tuple[int, ...]]]) -> None:
    while True:
        try:
            events.get_nowait()
        except queue.Empty:
            return


def _ingest_pending(
    control: ExpectedControl,
    events: queue.Queue[tuple[str, tuple[int, ...]]],
    observed: list[dict[str, Any]],
    seen_pairs: set[tuple[int, int]],
) -> bool:
    """Drain the queue into ``observed``; report whether a note release landed.

    The release must not be the step's FIRST message: a button let go during the
    previous prompt would otherwise end this step before it began.
    """
    release_seen = False
    while True:
        try:
            stamp, data = events.get_nowait()
        except queue.Empty:
            return release_seen
        decoded = decode_message(data)
        if decoded is None:
            continue
        if control.kind == "cc":
            # One record per DISTINCT wire pair: a 14-bit control sends its MSB
            # and LSB on two different CC numbers, and both matter.
            pair = (decoded.status, decoded.code)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            observed.append(_observed_record(stamp, data, decoded))
            continue
        first = not observed
        observed.append(_observed_record(stamp, data, decoded))
        if not first and is_note_release(decoded):
            release_seen = True


def _await_control(
    control: ExpectedControl,
    events: queue.Queue[tuple[str, tuple[int, ...]]],
    keys: _KeyPoller,
) -> tuple[str, list[dict[str, Any]]]:
    """Collect one control's traffic. Returns an action plus what was observed.

    Actions: ``captured``, ``skipped``, ``quit``, ``back``.
    """
    observed: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, int]] = set()
    deadline: float | None = None
    window = CC_WINDOW_S if control.kind == "cc" else NOTE_RELEASE_TIMEOUT_S
    while True:
        key = keys.poll(POLL_INTERVAL_S)
        action = KEY_ACTIONS.get(key) if key is not None else None
        if action == "quit":
            return "quit", observed
        if action == "back":
            return "back", observed
        if action == "skip":
            return ("captured" if observed else "skipped"), observed
        released = _ingest_pending(control, events, observed, seen_pairs)
        if observed and deadline is None:
            deadline = time.monotonic() + window
        if released:
            return "captured", observed
        if deadline is not None and time.monotonic() >= deadline:
            return "captured", observed


# ---------------------------------------------------------------- subcommands


def cmd_ports(_args: argparse.Namespace) -> int:
    _midi_in, names = _list_ports()
    if not names:
        print(
            "error: CoreMIDI reports zero input ports. Is the controller attached?",
            file=sys.stderr,
        )
        return 2
    for i, name in enumerate(names):
        print(f"[{i}] {name}")
    print(f"{len(names)} input port(s)")
    return 0


def cmd_sniff(args: argparse.Namespace) -> int:
    expected_map = load_expected_map(args.map)
    midi_in, port_name = _open_port(args.port)
    events: queue.Queue[tuple[str, tuple[int, ...]]] = queue.Queue()
    midi_in.set_callback(_make_callback(events))
    print(f"listening on {port_name!r} against {expected_map.path.name} (Ctrl-C to stop)")
    mapped: set[tuple[int, int]] = set()
    unmapped: set[tuple[int, int]] = set()
    try:
        while True:
            try:
                stamp, data = events.get(timeout=0.2)
            except queue.Empty:
                continue
            decoded = decode_message(data)
            if decoded is None:
                continue
            pair = (decoded.status, decoded.code)
            control = lookup_control(expected_map.index, *pair)
            if control is None:
                unmapped.add(pair)
                label = "UNMAPPED"
            else:
                mapped.add(pair)
                label = f"{control.id} ({control.name})"
            raw = " ".join(f"{b:02X}" for b in data)
            print(
                f"{stamp}  {raw:<12}  {decoded.kind} ch{decoded.channel} "
                f"code {decoded.code} value {decoded.value}  {label}"
            )
    except KeyboardInterrupt:
        print()
    finally:
        midi_in.close_port()
    print(f"distinct pairs: mapped {len(mapped)} / unmapped {len(unmapped)}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    expected_map = load_expected_map(args.map)
    selected = filter_controls(
        expected_map.controls,
        section=args.section,
        deck=args.deck,
        no_shift=args.no_shift,
        ids=[i.strip() for i in args.ids.split(",") if i.strip()] if args.ids else None,
    )
    if not selected:
        sections = sorted({c.section for c in expected_map.controls})
        raise SystemExit(
            f"error: filters selected zero controls. Sections in the map: {', '.join(sections)}"
        )
    midi_in, port_name = _open_port(args.port)
    events: queue.Queue[tuple[str, tuple[int, ...]]] = queue.Queue()
    midi_in.set_callback(_make_callback(events))
    doc: dict[str, Any] = {
        "device": expected_map.device,
        "port": port_name,
        "map_path": str(expected_map.path),
        "map_sha256": expected_map.sha256,
        "started_utc": _utc_now(),
        "entries": [],
    }
    _write_capture(args.out, doc)
    print(f"capturing {len(selected)} control(s) from {port_name!r} into {args.out}")
    print("keys: Enter or s = skip, b = back one, q = save and quit")
    position = 0
    try:
        with _cbreak_stdin():
            keys = _KeyPoller()
            while position < len(selected):
                control = selected[position]
                print(_prompt_for(control, position + 1, len(selected)))
                _drain(events)
                action, observed = _await_control(control, events, keys)
                if action == "back":
                    position = max(0, position - 1)
                    if doc["entries"]:
                        doc["entries"].pop()
                    _write_capture(args.out, doc)
                    continue
                expected = {
                    "status": control.status,
                    "ch": control.ch,
                    "code": control.code,
                    "kind": control.kind,
                }
                verdict = compute_verdict(expected, observed, action == "skipped")
                doc["entries"].append(
                    {
                        "id": control.id,
                        "expected": expected,
                        "observed": observed,
                        "verdict": verdict,
                    }
                )
                _write_capture(args.out, doc)
                print(f"    -> {verdict}: {_format_observed(observed)}")
                if action == "quit":
                    break
                position += 1
    except KeyboardInterrupt:
        print()
    finally:
        midi_in.close_port()
    _write_capture(args.out, doc)
    print(f"wrote {len(doc['entries'])} entr(ies) to {args.out}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    expected_map = load_expected_map(args.map)
    if not args.capture.is_file():
        raise SystemExit(f"error: capture file not found at {args.capture}")
    capture = json.loads(args.capture.read_text(encoding="utf-8"))
    report = build_diff_report(capture, expected_map)
    widths = (
        max([len("id")] + [len(r.control_id) for r in report.rows]),
        max([len("expected")] + [len(r.expected_text) for r in report.rows]),
        max([len("observed")] + [len(r.observed_text) for r in report.rows]),
    )
    header = (
        f"{'id':<{widths[0]}} | {'expected':<{widths[1]}} | "
        f"{'observed':<{widths[2]}} | verdict"
    )
    print(header)
    print("-" * len(header))
    for row in report.rows:
        print(
            f"{row.control_id:<{widths[0]}} | {row.expected_text:<{widths[1]}} | "
            f"{row.observed_text:<{widths[2]}} | {row.verdict}"
        )
    for status, code in report.extras:
        print(f"EXTRA 0x{status:02X}/{code} matches no expected control")
    print(report.summary_line)
    return 1 if report.mismatch else 0


# ---------------------------------------------------------------------- main


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capture.py", description="Live MIDI capture probe for controller map adjudication."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ports", help="list CoreMIDI input ports").set_defaults(func=cmd_ports)

    sniff = sub.add_parser("sniff", help="stream decoded inbound messages")
    sniff.add_argument("--map", type=Path, default=DEFAULT_MAP_PATH)
    sniff.add_argument("--port", default=DEFAULT_PORT_NEEDLE, help="port name substring")
    sniff.set_defaults(func=cmd_sniff)

    capture = sub.add_parser("capture", help="guided walk of the expected controls")
    capture.add_argument("--out", type=Path, required=True)
    capture.add_argument("--map", type=Path, default=DEFAULT_MAP_PATH)
    capture.add_argument("--port", default=DEFAULT_PORT_NEEDLE, help="port name substring")
    capture.add_argument("--section", default=None, help="deck, mixer, browse, performance, effect")
    capture.add_argument("--deck", type=int, default=None, choices=(1, 2))
    capture.add_argument("--no-shift", action="store_true", help="exclude +SHIFT rows")
    capture.add_argument("--ids", default=None, help="comma separated control ids")
    capture.set_defaults(func=cmd_capture)

    diff = sub.add_parser("diff", help="grade a capture file against the expected map")
    diff.add_argument("capture", type=Path)
    diff.add_argument("--map", type=Path, default=DEFAULT_MAP_PATH)
    diff.set_defaults(func=cmd_diff)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Line buffering, always: `sniff` and `capture` are live instruments whose
    # output is routinely piped to a log or a `timeout`. Block buffering there
    # means a killed process flushes nothing, and a silent log reads exactly
    # like a port that never opened.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = _build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
