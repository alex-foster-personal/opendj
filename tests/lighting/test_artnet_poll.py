"""LIGHT-22 Art-Net ArtPoll discovery and subscriber table acceptance tests (issue #9875).

[if] an ArtPollReply is misread, a silent node outlives 3 polls, a short reply passes, or an unsubscribed Port-Address yields any destination [then] fail, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.lighting.artnet import (
    ARTNET_ID,
    PollReplyView,
    decode_pollreply,
    encode_artdmx,
    port_address,
)
from apps.lighting.artnet import (
    encode_artpoll as encode_artpoll_discovery,
)
from apps.lighting.artnet_poll import (
    ARTNET_PORT,
    ARTPOLL_LENGTH,
    ARTPOLLREPLY_MIN_LENGTH,
    MISSED_POLLS_BEFORE_EXPIRY,
    OPCODE_ARTPOLL,
    NodeInfo,
    SubscriberTable,
    encode_artpoll,
    parse_artpollreply,
)

pytestmark = [pytest.mark.requirement("LIGHT-10462")]

FIXTURE = Path(__file__).parent / "fixtures" / "artpollreply_two_ports.bin"
BROADCAST_SUFFIX = ".255"


def _node(ip: str, *outputs: int, bind_index: int = 1) -> NodeInfo:
    return NodeInfo(
        ip=ip,
        port=ARTNET_PORT,
        short_name=f"node-{ip}",
        long_name="",
        output_port_addresses=tuple(outputs),
        bind_index=bind_index,
    )


def test_recorded_reply_with_two_output_ports_gives_both_port_addresses_and_the_short_name() -> None:
    """[if] a recorded ArtPollReply with two output ports is parsed [then] both Port-Addresses and the short name are returned, [else stop]."""
    node = parse_artpollreply(FIXTURE.read_bytes())
    assert node.short_name == "ODJ-Node-1"
    assert node.long_name == "Open DJ test node with two DMX outputs"
    assert node.ip == "10.0.0.7"
    assert node.port == ARTNET_PORT == 6454
    assert node.output_port_addresses == (port_address(1, 2, 3), port_address(1, 2, 4))
    assert node.bind_index == 1


def test_a_node_that_stops_replying_for_three_poll_intervals_leaves_the_table() -> None:
    """[if] a node stops replying for 3 poll intervals [then] it leaves the table, [else stop]."""
    table = SubscriberTable(poll_interval_s=2.5)
    universe = port_address(0, 0, 1)
    table.record(_node("10.0.0.7", universe), now=0.0)
    table.record(_node("10.0.0.8", universe), now=0.0)
    table.record(_node("10.0.0.8", universe), now=5.0)
    just_inside = MISSED_POLLS_BEFORE_EXPIRY * 2.5
    assert sorted(table.destinations(universe, now=just_inside)) == [("10.0.0.7", 6454), ("10.0.0.8", 6454)]
    assert table.destinations(universe, now=just_inside + 0.1) == [("10.0.0.8", 6454)]
    assert [node.ip for node in table.nodes(now=5.0 + just_inside + 0.1)] == []


def test_a_port_address_with_no_subscriber_has_no_destination_and_never_a_broadcast() -> None:
    """[if] destinations() is asked for a universe with no subscriber [then] it returns an empty list, never a broadcast address, [else stop]."""
    table = SubscriberTable()
    table.record(_node("10.0.0.7", port_address(0, 0, 1)), now=0.0)
    unsubscribed = table.destinations(port_address(0, 0, 2), now=0.0)
    assert unsubscribed == []
    assert table.destinations(port_address(0, 0, 9), now=0.0) == []
    for ip, _port in table.destinations(port_address(0, 0, 1), now=0.0):
        assert not ip.endswith(BROADCAST_SUFFIX)


def test_a_reply_shorter_than_the_minimum_raises_with_the_received_length() -> None:
    """[if] a reply is shorter than the minimum size [then] parse raises with the received length, [else stop]."""
    short = FIXTURE.read_bytes()[:100]
    with pytest.raises(ValueError, match=f"received {len(short)}") as info:
        parse_artpollreply(short)
    assert str(ARTPOLLREPLY_MIN_LENGTH) in str(info.value)


def test_wrong_id_or_opcode_is_refused() -> None:
    """[if] the ID or OpCode is not ArtPollReply [then] parse raises naming the field, [else stop]."""
    reply = bytearray(FIXTURE.read_bytes())
    with pytest.raises(ValueError, match="OpCode is 0x5000"):
        parse_artpollreply(bytes(reply[:8]) + (0x5000).to_bytes(2, "little") + bytes(reply[10:]))
    reply[0:8] = b"Not-Art\x00"
    with pytest.raises(ValueError, match="not an Art-Net packet"):
        parse_artpollreply(bytes(reply))


def test_artpoll_packet_layout() -> None:
    """[if] ArtPoll is encoded [then] it is 14 bytes with the ID, OpCode 0x2000 low byte first and ProtVer 14, [else stop]."""
    packet = encode_artpoll()
    assert len(packet) == ARTPOLL_LENGTH == 14
    assert packet[:8] == ARTNET_ID
    assert int.from_bytes(packet[8:10], "little") == OPCODE_ARTPOLL
    assert int.from_bytes(packet[10:12], "big") == 14
    assert packet[12] & 0x02
    assert encode_artpoll(reply_on_change=False)[12] == 0


def test_poll_interval_outside_the_spec_range_is_refused() -> None:
    """[if] the poll interval is outside 2.5..3 s [then] ValueError names the range, [else stop]."""
    with pytest.raises(ValueError, match="2.5..3.0"):
        SubscriberTable(poll_interval_s=5.0)


# --- issue #11776: discovery facade on apps.lighting.artnet (rename LIGHT id when PR is open)


@pytest.mark.requirement("LIGHT-11776")
def test_discovery_encode_artpoll_has_art_net_id_and_opcode_2000() -> None:
    """[if] encode_artpoll runs on artnet [then] ID is Art-Net and opcode is 0x2000, [else stop]."""
    packet = encode_artpoll_discovery()
    assert packet[:8] == ARTNET_ID
    assert int.from_bytes(packet[8:10], "little") == OPCODE_ARTPOLL


@pytest.mark.requirement("LIGHT-11776")
def test_discovery_decode_pollreply_matches_239_byte_fixture() -> None:
    """[if] decode_pollreply gets the 239-byte fixture [then] ip, short_name and universe match, [else stop]."""
    # Fixture: tests/lighting/fixtures/artpollreply_two_ports.bin (239 bytes); ip 10.0.0.7, ODJ-Node-1
    view = decode_pollreply(FIXTURE.read_bytes())
    assert view == PollReplyView(
        ip="10.0.0.7",
        short_name="ODJ-Node-1",
        universe=port_address(1, 2, 3),
    )


@pytest.mark.requirement("LIGHT-11776")
def test_discovery_decode_pollreply_returns_none_for_artdmx() -> None:
    """[if] decode_pollreply gets ArtDmx [then] it returns None, [else stop]."""
    artdmx = encode_artdmx(port_address(0, 0, 1), b"\x00\x01", sequence=0)
    assert decode_pollreply(artdmx) is None


@pytest.mark.requirement("LIGHT-11776")
def test_discovery_decode_pollreply_returns_none_for_bad_id() -> None:
    """[if] the ID is not Art-Net [then] decode_pollreply returns None, [else stop]."""
    reply = bytearray(FIXTURE.read_bytes())
    reply[0:8] = b"Not-Art\x00"
    assert decode_pollreply(bytes(reply)) is None
