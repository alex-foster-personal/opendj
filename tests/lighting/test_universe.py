"""LIGHT-9941: DMX universe buffer, HTP and LTP merge, and the send policy.

[if] HTP or LTP merge picks the wrong layer, the policy breaks 44 Hz or 800..1000 ms, or a value is clamped [then] fail, [else stop].
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from apps.lighting.universe import (
    FIRST_SEND_PACKETS,
    SLOT_COUNT,
    Layer,
    MergeRule,
    SendPolicy,
    Universe,
    merge,
)

pytestmark = [pytest.mark.requirement("LIGHT-9941")]


def test_htp_keeps_the_highest_value() -> None:
    """[if] two layers set slot 1 to 100 and 200 under HTP [then] slot 1 is 200."""
    layers = [Layer("a", {1: 100}, 2.0), Layer("b", {1: 200}, 1.0)]
    frame = merge(layers, {1: MergeRule.HTP}, default=MergeRule.LTP)
    assert frame[0] == 200
    assert len(frame) == SLOT_COUNT


def test_ltp_keeps_the_latest_layer() -> None:
    """[if] two layers set slot 10 under LTP [then] the layer with the later timestamp wins."""
    early_last = [Layer("late", {10: 30}, 5.0), Layer("early", {10: 250}, 1.0)]
    assert merge(early_last, {}, default=MergeRule.LTP)[9] == 30
    late_last = [Layer("early", {10: 250}, 1.0), Layer("late", {10: 30}, 5.0)]
    assert merge(late_last, {}, default=MergeRule.LTP)[9] == 30


def test_ltp_tie_goes_to_the_later_layer_in_order() -> None:
    """[if] two LTP layers share a timestamp [then] the one listed last wins."""
    layers = [Layer("a", {10: 1}, 1.0), Layer("b", {10: 2}, 1.0)]
    assert merge(layers, {10: MergeRule.LTP}, default=MergeRule.HTP)[9] == 2


def test_changing_data_sends_at_most_44_frames_a_second() -> None:
    """[if] data changes every 5 ms for 1 s [then] the policy allows at most 44 sends."""
    policy = SendPolicy()
    sends = 0
    for step in range(201):
        now = step * 0.005
        frame = bytes([step % 256]) + bytes(SLOT_COUNT - 1)
        if policy.packets_due(now, frame):
            sends += 1
    assert 0 < sends <= 44


def test_unchanged_data_keeps_alive_every_800_to_1000_ms() -> None:
    """[if] data is unchanged for 3 s [then] the policy allows a send every 800 to 1000 ms and no more."""
    policy = SendPolicy()
    frame = bytes(SLOT_COUNT)
    send_times: list[float] = []
    for step in range(601):
        now = step * 0.005
        if policy.packets_due(now, frame):
            send_times.append(now)
    gaps = [later - earlier for earlier, later in pairwise(send_times)]
    assert len(gaps) >= 2
    assert all(0.8 <= gap <= 1.0 for gap in gaps), gaps


def test_first_send_is_three_identical_packets() -> None:
    """[if] a universe is sent for the first time [then] 3 packets go out, then 1 per later send."""
    policy = SendPolicy()
    frame = bytes(SLOT_COUNT)
    assert policy.packets_due(0.0, frame) == FIRST_SEND_PACKETS == 3
    assert policy.packets_due(0.95, frame) == 1


def test_out_of_range_value_raises_never_clamps() -> None:
    """[if] a slot value outside 0..255 is written [then] ValueError, never a silent clamp."""
    universe = Universe(1)
    with pytest.raises(ValueError):
        universe.set(1, 256)
    with pytest.raises(ValueError):
        universe.set(1, -1)
    with pytest.raises(ValueError):
        Layer("a", {1: 300}, 0.0)
    assert universe.get(1) == 0


def test_out_of_range_slot_and_backwards_time_raise() -> None:
    """[if] a slot outside 1..512 is used or time runs backwards [then] ValueError."""
    with pytest.raises(ValueError):
        Universe(1).set(513, 1)
    policy = SendPolicy()
    policy.packets_due(1.0, bytes(SLOT_COUNT))
    with pytest.raises(ValueError):
        policy.packets_due(0.5, bytes(SLOT_COUNT))


@pytest.mark.requirement("TESTINFRA-14132")
def test_zero_length_frame_refused_before_scheduling() -> None:
    """[if] a zero-length frame hits the send policy [then] ValueError before any packet decision."""
    policy = SendPolicy()
    with pytest.raises(ValueError, match="512 slots"):
        policy.packets_due(0.0, b"")
    assert policy.packets_due(0.0, bytes(SLOT_COUNT)) == FIRST_SEND_PACKETS


@pytest.mark.requirement("TESTINFRA-14132")
def test_oversized_frame_refused_before_scheduling() -> None:
    """[if] a frame longer than one universe hits the policy [then] ValueError before scheduling."""
    policy = SendPolicy()
    with pytest.raises(ValueError, match="512 slots"):
        policy.packets_due(0.0, bytes(SLOT_COUNT + 1))


@pytest.mark.requirement("TESTINFRA-14132")
def test_changing_frame_length_after_send_refused() -> None:
    """[if] frame length changes after a prior send [then] ValueError and no new packets."""
    policy = SendPolicy()
    frame = bytes(SLOT_COUNT)
    assert policy.packets_due(0.0, frame) == FIRST_SEND_PACKETS
    short = bytes(100)
    with pytest.raises(ValueError, match="512 slots"):
        policy.packets_due(1.0, short)
    assert policy.packets_due(1.0, frame) == 1


@pytest.mark.requirement("TESTINFRA-14132")
def test_backwards_time_after_send_raises_without_emitting() -> None:
    """[if] time regresses after a send [then] ValueError and scheduling can continue forward."""
    policy = SendPolicy()
    frame = bytes(SLOT_COUNT)
    assert policy.packets_due(1.0, frame) == FIRST_SEND_PACKETS
    with pytest.raises(ValueError, match="backwards"):
        policy.packets_due(0.5, frame)
    assert policy.packets_due(1.1, frame) == 0


@pytest.mark.requirement("TESTINFRA-14132")
def test_unchanged_frame_at_keepalive_threshold_sends_one_packet() -> None:
    """[if] an unchanged frame hits the keep-alive interval [then] exactly one packet is due."""
    policy = SendPolicy(keepalive_s=0.9)
    frame = bytes(SLOT_COUNT)
    assert policy.packets_due(0.0, frame) == FIRST_SEND_PACKETS
    assert policy.packets_due(0.9, frame) == 1
    assert policy.packets_due(0.91, frame) == 0
