"""Art-Net 4 ArtPoll discovery and the subscriber table (LIGHT-22 in the lighting spec).

Clean-room from the public Art-Net 4 specification (art-net.org.uk). Pure: no sockets.

Art-Net 4 requires ArtDmx to be unicast to subscribed nodes, never broadcast. A controller
learns its subscribers by broadcasting ArtPoll every 2.5 to 3 seconds and reading each node's
ArtPollReply, which names the node's address and the Port-Addresses its output ports carry.
``SubscriberTable`` keeps those nodes and forgets one after three poll intervals without a
reply. A Port-Address with no subscriber has no destination: ``destinations`` returns an
empty list and the caller logs that once, rather than falling back to a broadcast address.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.lighting.artnet import ARTNET_ID, PROTOCOL_VERSION

OPCODE_ARTPOLL = 0x2000
OPCODE_ARTPOLLREPLY = 0x2100
ARTNET_PORT = 0x1936

ARTPOLL_LENGTH = 14
FLAG_REPLY_ON_CHANGE = 0x02
DIAG_PRIORITY_LOW = 0x10

POLL_INTERVAL_MIN_S = 2.5
POLL_INTERVAL_MAX_S = 3.0
POLL_INTERVAL_DEFAULT_S = 2.8
MISSED_POLLS_BEFORE_EXPIRY = 3

MAX_PORTS_PER_REPLY = 4
PORT_TYPE_OUTPUT_FROM_ARTNET = 0x80

# ArtPollReply field offsets (bytes) from the Art-Net 4 packet definition.
_OFF_OPCODE = 8
_OFF_IP = 10
_OFF_PORT = 14
_OFF_NET_SWITCH = 18
_OFF_SUB_SWITCH = 19
_OFF_SHORT_NAME = 26
_SHORT_NAME_LENGTH = 18
_OFF_LONG_NAME = 44
_LONG_NAME_LENGTH = 64
_OFF_NUM_PORTS = 172
_OFF_PORT_TYPES = 174
_OFF_SW_OUT = 190
_OFF_BIND_INDEX = 211
ARTPOLLREPLY_MIN_LENGTH = _OFF_BIND_INDEX + 1


class ArtPollReplyPacketError(ValueError):
    """An ArtPollReply that fails the output-port table or other packet layout rules."""

    def __init__(self, field_name: str, offset: int, detail: str) -> None:
        super().__init__(f"{field_name} at offset {offset}: {detail}")
        self.field_name = field_name
        self.offset = offset


@dataclass(frozen=True)
class NodeInfo:
    """What one ArtPollReply says about a node."""

    ip: str
    port: int
    short_name: str
    long_name: str
    output_port_addresses: tuple[int, ...]
    bind_index: int


# ----------------------------------------------------------------------------- helpers


def _c_string(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _require_poll_interval(poll_interval_s: float) -> None:
    if not POLL_INTERVAL_MIN_S <= poll_interval_s <= POLL_INTERVAL_MAX_S:
        raise ValueError(
            f"poll_interval_s {poll_interval_s} is outside {POLL_INTERVAL_MIN_S}..{POLL_INTERVAL_MAX_S} (Art-Net 4)"
        )


def _output_port_table_end(port_count: int) -> int:
    return max(_OFF_PORT_TYPES + port_count, _OFF_SW_OUT + port_count)


def _read_output_port_address(data: bytes, index: int) -> int:
    net = data[_OFF_NET_SWITCH] & 0x7F
    subnet = data[_OFF_SUB_SWITCH] & 0x0F
    return (net << 8) | (subnet << 4) | (data[_OFF_SW_OUT + index] & 0x0F)


# ----------------------------------------------------------------------------- public api


def encode_artpoll(*, reply_on_change: bool = True) -> bytes:
    """One ArtPoll packet: ID, OpCode 0x2000, ProtVer 14, Flags and DiagPriority.

    ``reply_on_change`` sets Flags bit 1 so nodes also reply when their state changes, which keeps
    the subscriber table fresh between polls. Diagnostics are not requested.
    """
    flags = FLAG_REPLY_ON_CHANGE if reply_on_change else 0
    return (
        ARTNET_ID
        + OPCODE_ARTPOLL.to_bytes(2, "little")
        + PROTOCOL_VERSION.to_bytes(2, "big")
        + bytes((flags, DIAG_PRIORITY_LOW))
    )


def parse_artpollreply(data: bytes) -> NodeInfo:
    """Read a node's address, names and output Port-Addresses from an ArtPollReply.

    An output Port-Address is Net (NetSwitch, 7 bits) : Sub-Net (SubSwitch, 4 bits) : Universe
    (SwOut[n], 4 bits) for each of the first NumPorts ports whose PortTypes byte says it outputs
    DMX from Art-Net.
    """
    if len(data) < _OFF_NUM_PORTS + 2:
        raise ValueError(f"ArtPollReply needs at least {ARTPOLLREPLY_MIN_LENGTH} bytes, received {len(data)}")
    if data[:8] != ARTNET_ID:
        raise ValueError(f"not an Art-Net packet: ID is {data[:8]!r}")
    opcode = int.from_bytes(data[_OFF_OPCODE : _OFF_OPCODE + 2], "little")
    if opcode != OPCODE_ARTPOLLREPLY:
        raise ValueError(f"not an ArtPollReply: OpCode is 0x{opcode:04X}, expected 0x{OPCODE_ARTPOLLREPLY:04X}")
    port_count = min(int.from_bytes(data[_OFF_NUM_PORTS : _OFF_NUM_PORTS + 2], "big"), MAX_PORTS_PER_REPLY)
    port_table_end = _output_port_table_end(port_count)
    if len(data) < port_table_end:
        raise ArtPollReplyPacketError(
            "output-port table",
            _OFF_SW_OUT,
            f"truncated inside output-port fields: need {port_table_end} bytes, received {len(data)}",
        )
    if len(data) < ARTPOLLREPLY_MIN_LENGTH:
        raise ValueError(f"ArtPollReply needs at least {ARTPOLLREPLY_MIN_LENGTH} bytes, received {len(data)}")
    outputs = tuple(
        address
        for index in range(port_count)
        if data[_OFF_PORT_TYPES + index] & PORT_TYPE_OUTPUT_FROM_ARTNET
        for address in (_read_output_port_address(data, index),)
        if address != 0
    )
    return NodeInfo(
        ip=".".join(str(octet) for octet in data[_OFF_IP : _OFF_IP + 4]),
        port=int.from_bytes(data[_OFF_PORT : _OFF_PORT + 2], "little"),
        short_name=_c_string(data[_OFF_SHORT_NAME : _OFF_SHORT_NAME + _SHORT_NAME_LENGTH]),
        long_name=_c_string(data[_OFF_LONG_NAME : _OFF_LONG_NAME + _LONG_NAME_LENGTH]),
        output_port_addresses=outputs,
        bind_index=data[_OFF_BIND_INDEX],
    )


@dataclass
class SubscriberTable:
    """Nodes that subscribe to each Port-Address, keyed by node address and BindIndex.

    Time is an argument in seconds, as in ``SendPolicy``. A node expires after
    ``MISSED_POLLS_BEFORE_EXPIRY`` poll intervals without a reply.
    """

    poll_interval_s: float = POLL_INTERVAL_DEFAULT_S
    _nodes: dict[tuple[str, int], NodeInfo] = field(default_factory=dict, init=False)
    _last_reply_at: dict[tuple[str, int], float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        _require_poll_interval(self.poll_interval_s)

    @property
    def expiry_s(self) -> float:
        return MISSED_POLLS_BEFORE_EXPIRY * self.poll_interval_s

    def record(self, node: NodeInfo, now: float) -> None:
        """Store or refresh a node from its reply at time ``now``."""
        if not node.output_port_addresses:
            return
        key = (node.ip, node.bind_index)
        self._nodes[key] = node
        self._last_reply_at[key] = now

    def record_reply(self, data: bytes, now: float) -> None:
        """Parse one ArtPollReply and record the node only when it has usable output Port-Addresses."""
        self.record(parse_artpollreply(data), now)

    def expire(self, now: float) -> list[NodeInfo]:
        """Forget nodes silent for longer than ``expiry_s`` and return them."""
        gone = [key for key, seen in self._last_reply_at.items() if now - seen > self.expiry_s]
        removed = [self._nodes.pop(key) for key in gone]
        for key in gone:
            del self._last_reply_at[key]
        return removed

    def nodes(self, now: float) -> list[NodeInfo]:
        """Live nodes at time ``now``."""
        self.expire(now)
        return list(self._nodes.values())

    def destinations(self, port_address: int, now: float) -> list[tuple[str, int]]:
        """Unicast (ip, port) targets for ``port_address`` at time ``now``; empty when nobody subscribes."""
        return [(node.ip, node.port) for node in self.nodes(now) if port_address in node.output_port_addresses]
