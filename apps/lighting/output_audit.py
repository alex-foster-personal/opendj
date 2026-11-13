"""Audit a captured stream of lighting output packets against the output timing and addressing rules.

Pure functions over timestamped packets: no sockets, no clock. Packets are decoded with the
independent monitor decoders; cadence limits come from ``universe`` and sequence wrap from ``artnet``.
"""

from __future__ import annotations

import ipaddress
import itertools
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from apps.lighting.artnet import SEQUENCE_OFF, next_sequence
from apps.lighting.monitor import ARTNET_ID, DmxFrame, PacketError, decode_artdmx, decode_e131_data
from apps.lighting.sacn import OPTION_STREAM_TERMINATED
from apps.lighting.universe import FIRST_SEND_PACKETS, KEEPALIVE_MAX_S, KEEPALIVE_MIN_S, MAX_FRAMES_PER_SECOND

MIN_FRAMES_PER_SECOND = 40.0
TERMINATE_PACKETS = FIRST_SEND_PACKETS
LIMITED_BROADCAST = "255.255.255.255"


@dataclass(frozen=True)
class CapturedPacket:
    """One packet as captured: send time in seconds, destination host and raw bytes."""

    at: float
    destination: str
    data: bytes


@dataclass(frozen=True)
class Finding:
    """One audit failure: a short code, the universe it concerns and a human detail."""

    code: str
    universe: int | None
    detail: str


@dataclass(frozen=True)
class _Decoded:
    at: float
    destination: str
    frame: DmxFrame

    @property
    def terminated(self) -> bool:
        return self.frame.protocol == "sacn" and bool((self.frame.options or 0) & OPTION_STREAM_TERMINATED)


def _decode(packet: CapturedPacket) -> DmxFrame:
    if packet.data[:8] == ARTNET_ID:
        return decode_artdmx(packet.data)
    return decode_e131_data(packet.data)


def is_broadcast(destination: str) -> bool:
    """True for the limited broadcast address or an IPv4 address whose last octet is 255."""
    try:
        address = ipaddress.IPv4Address(destination)
    except ValueError:
        return False
    return str(address) == LIMITED_BROADCAST or address.packed[-1] == 0xFF


def _by_stream(packets: Iterable[_Decoded]) -> dict[tuple[str, int], list[_Decoded]]:
    streams: dict[tuple[str, int], list[_Decoded]] = {}
    for item in sorted(packets, key=lambda p: p.at):
        streams.setdefault((item.frame.protocol, item.frame.universe), []).append(item)
    return streams


def _sends(stream: list[_Decoded]) -> list[_Decoded]:
    """Collapse same-instant identical bursts (first send) into one send; drop terminate packets."""
    sends: list[_Decoded] = []
    for item in stream:
        if item.terminated:
            continue
        if sends and sends[-1].at == item.at and sends[-1].frame.slots == item.frame.slots:
            continue
        sends.append(item)
    return sends


def check_cadence(stream: list[_Decoded]) -> list[Finding]:
    """Changed data must run at 40 to 44 fps; unchanged data must repeat every 800 to 1000 ms."""
    findings: list[Finding] = []
    sends = _sends(stream)
    run: list[_Decoded] = []

    def close_run() -> None:
        if len(run) >= 2:
            span = run[-1].at - run[0].at
            fps = (len(run) - 1) / span if span > 0 else float("inf")
            if not MIN_FRAMES_PER_SECOND <= fps <= MAX_FRAMES_PER_SECOND:
                findings.append(Finding("cadence_active", run[0].frame.universe, f"{fps:.1f} fps while data changes"))
        run.clear()

    for previous, current in itertools.pairwise(sends):
        if current.frame.slots != previous.frame.slots:
            if not run:
                run.append(previous)
            run.append(current)
            continue
        close_run()
        gap = current.at - previous.at
        if not KEEPALIVE_MIN_S - 1e-9 <= gap <= KEEPALIVE_MAX_S + 1e-9:
            findings.append(Finding("cadence_idle", current.frame.universe, f"idle repeat after {gap * 1000:.0f} ms"))
    close_run()
    return findings


def check_sequence(stream: list[_Decoded]) -> list[Finding]:
    """Sequence numbers must be consecutive per universe (Art-Net skips 0, sACN wraps 255 to 0)."""
    findings: list[Finding] = []
    for previous, current in itertools.pairwise(stream):
        before, after = previous.frame.sequence, current.frame.sequence
        if current.frame.protocol == "artnet":
            if SEQUENCE_OFF in (before, after):
                continue
            expected = next_sequence(before)
        else:
            expected = (before + 1) % 256
        if after != expected:
            findings.append(Finding("sequence_gap", current.frame.universe, f"sequence {before} then {after}"))
    return findings


def check_termination(stream: list[_Decoded]) -> list[Finding]:
    """An sACN stream must end with exactly 3 stream-terminated packets."""
    if not stream or stream[0].frame.protocol != "sacn":
        return []
    count = sum(1 for item in stream if item.terminated)
    tail = 0
    for item in reversed(stream):
        if not item.terminated:
            break
        tail += 1
    if count != TERMINATE_PACKETS or tail != count:
        universe = stream[0].frame.universe
        return [Finding("terminate_count", universe, f"{count} terminate packets, {tail} at the end")]
    return []


def audit(packets: Iterable[CapturedPacket], destinations: Mapping[int, Collection[str]]) -> list[Finding]:
    """Run every output check over captured packets; ``destinations`` maps universe to configured hosts."""
    findings: list[Finding] = []
    decoded: list[_Decoded] = []
    for packet in packets:
        try:
            frame = _decode(packet)
        except PacketError as exc:
            findings.append(Finding("rejected", None, str(exc)))
            continue
        decoded.append(_Decoded(packet.at, packet.destination, frame))
        if frame.protocol == "artnet" and is_broadcast(packet.destination):
            findings.append(Finding("artdmx_broadcast", frame.universe, f"ArtDmx sent to {packet.destination}"))
    unconfigured: set[int] = set()
    for (_protocol, universe), stream in _by_stream(decoded).items():
        if not destinations.get(universe) and universe not in unconfigured:
            unconfigured.add(universe)
            findings.append(Finding("no_destination", universe, f"universe {universe} has no configured destination"))
        findings.extend(check_cadence(stream))
        findings.extend(check_sequence(stream))
        findings.extend(check_termination(stream))
    return findings
