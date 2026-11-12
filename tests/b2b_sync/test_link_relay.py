"""B2BRELAY-01, 03, 05: the Link relay's decisions, its sealed wire, and two relays in one process.

Pure decisions are table-tested in BOTH directions (the engage rule and its overshoot). The
in-process pair runs two real ``LinkRelay`` services through two real B2B ``Door`` servers on
loopback, each over its own session (``carabiner_sim.SimCarabiner``: Carabiner's line protocol
with Link's timeline math). The leader's wire clock is injected (+1.234567 s, +200 ppm), so a
follower that ignored the offset or the skew would be seconds or many ms out.

- [if] two relays run over two doors with Link blocked [then] the follower locks to the leader and writes nothing once Link sees a peer, [else stop]
- [if] an idle relay engages on anything but multicast_blocked, or a relay stays engaged while
  Link counts a peer [then] broken
- [if] an engaged relay drops on a lost path (peer_unreachable) [then] the tempo is not held (ADR-0124)
- [if] the bar phase is compared on raw beats rather than modulo the quantum [then] broken
- [if] a frame sealed for another pairing, tampered, or an answer replayed onto a new request opens [then] broken
- [if] a sealed relay request uses ordinary bearer-authenticated POST [then] the pairing token crosses the wire
- [if] the follower is not within 1 ms of the leader's bar and at its tempo after engaging [then] broken
- [if] a leader tempo change does not reach the follower within 0.5 s [then] broken
- [if] the follower writes Link while its Link counts a peer [then] two paths run at once: broken
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
import pytest

from apps.ableton_link import carabiner as carabiner_mod
from apps.ableton_link.carabiner import CarabinerClient
from apps.ableton_link.link_clock import beat_at_time, link_host_clock_available, link_now_us
from apps.b2b_pairing.b2b_door import Door
from apps.b2b_sync import relay_clock
from apps.b2b_sync.link_relay import (
    CFG,
    AlignPlan,
    LeaderState,
    LinkRelay,
    RelayWireError,
    Timeline,
    WireClock,
    answer_aad,
    next_engaged,
    plan_alignment,
    relay_key,
    relay_mode,
    request_aad,
    seal,
    unseal,
)
from apps.b2b_sync.relay_clock import ClockEstimate
from apps.b2b_transport.channel import ChannelKind, LanChannel, Timeout
from tests.ableton_link.carabiner_sim import SimCarabiner

pytestmark = [pytest.mark.requirement("B2BRELAY-01")]

TOKEN = "relay-test-token-0123456789abcdef"
#: The in-process pair injects 200 ppm (4x a bad crystal), while the tolerance remains half that
#: signal. Ignoring skew is therefore still rejected, without treating shared-runner HTTP jitter as
#: a product failure; the estimator's exact accuracy has deterministic coverage in test_relay_clock.
INJECTED_SKEW_PPM = 200.0
SKEW_BPM_TOL = 124.0 * 100e-6
#: Two tempos within this are the same proposal (B2BSLO-03's TEMPO_MATCH_BPM on af--b2b-continuity-slos).
TEMPO_MATCH_BPM = 0.05
HOST_CLOCK_REQUIRED = pytest.mark.skipif(
    not link_host_clock_available(),
    reason="Ableton Link host clock is unavailable on this platform",
)


# ---------------------------------------------------------------- engage rule


@pytest.mark.parametrize(
    ("engaged", "peers", "verdict", "expected"),
    [
        (False, 0, "multicast_blocked", True),
        (False, 0, "waiting", False),
        (False, 0, "peer_unreachable", False),  # overshoot control: an idle relay does not engage on a dead path
        (False, 0, "unknown", False),
        (False, 0, "ok", False),
        (True, 0, "peer_unreachable", True),  # a lost path holds (ADR-0124)
        (True, 0, "waiting", True),
        (True, 0, "unknown", True),
        (True, 0, "ok", False),
        (True, 0, "link_off_remote", False),
        (True, 0, "local_network_blocked", False),
        (True, 1, "multicast_blocked", False),  # Link sees a peer: one source of truth
        (False, 2, "multicast_blocked", False),
    ],
)
def test_engage_rule_in_both_directions(engaged: bool, peers: int, verdict: str, expected: bool) -> None:
    got, reason = next_engaged(engaged, paired=True, joined=True, peers=peers, verdict=verdict)
    assert got is expected, f"if engaged={engaged} peers={peers} verdict={verdict} gives {got} then broken: {reason}"


def test_not_paired_or_not_joined_never_engages() -> None:
    assert next_engaged(True, paired=False, joined=True, peers=0, verdict="multicast_blocked")[0] is False
    assert next_engaged(True, paired=True, joined=False, peers=0, verdict="multicast_blocked")[0] is False


@pytest.mark.requirement("B2BRELAY-03")
@pytest.mark.parametrize(
    ("engaged", "role", "peer_role", "mode"),
    [
        (False, "following", "on_air", "idle"),
        (True, "following", "on_air", "follow"),
        (True, "on_air", "following", "lead"),
        (True, "on_air", None, "lead"),
        (True, "on_air", "on_air", "clash"),
        (True, "following", "following", "no_on_air"),
        (True, "following", None, "no_on_air"),
    ],
)
def test_only_following_an_on_air_peer_follows(engaged: bool, role: str, peer_role: str | None, mode: str) -> None:
    assert relay_mode(engaged, role, peer_role) == mode


# ---------------------------------------------------------------- alignment math


@pytest.mark.requirement("B2BRELAY-03")
def test_alignment_maps_through_the_offset_and_wraps_at_the_bar() -> None:
    now, offset = 1e9, 1_234_567.0
    leader = Timeline(bpm=128.0, start_us=now + offset - 500e6)  # leader clock reads now+offset
    same = Timeline(bpm=128.0, start_us=now - 500e6)
    plan = plan_alignment(same, leader, offset, 0.0, now)
    assert abs(plan.phase_error_ms) < 1e-6 and plan.force_beat is None and not plan.set_bpm, plan
    beat_ms = 60_000.0 / 128.0
    late = Timeline(bpm=128.0, start_us=same.start_us + 3.9 * beat_ms * 1000)  # 3.9 beats late = 0.1 early mod 4
    plan = plan_alignment(late, leader, offset, 0.0, now)
    assert plan.phase_error_ms == pytest.approx(0.1 * beat_ms, abs=1e-6), f"if not wrapped mod the quantum then broken: {plan}"
    assert plan.force_beat is not None
    fixed = Timeline(bpm=128.0, start_us=plan.at_us - plan.force_beat * 60e6 / 128.0)
    assert abs(plan_alignment(fixed, leader, offset, 0.0, now).phase_error_ms) < 1e-6, "if the force does not land then broken"
    edge = Timeline(bpm=128.0, start_us=same.start_us - (CFG.DEADBAND_MS * 0.9) * 1000)
    assert plan_alignment(edge, leader, offset, 0.0, now).force_beat is None, "if inside the deadband forces then broken"


def test_tempo_is_converted_through_the_skew() -> None:
    plan = plan_alignment(Timeline(128.0, 0.0), Timeline(128.0 / 1.00005, 0.0), 0.0, 50.0, 1e9)
    assert plan.target_bpm == pytest.approx(128.0, abs=1e-9) and not plan.set_bpm, plan


# ---------------------------------------------------------------- the sealed wire


def test_sealed_frames_refuse_another_pairing_tampering_and_replayed_answers() -> None:
    key, other = relay_key(TOKEN), relay_key(TOKEN + "x")
    envelope, nonce = seal(key, {"t_us": 1}, request_aad(CFG.PING_PATH))
    assert unseal(key, envelope, request_aad(CFG.PING_PATH))[0] == {"t_us": 1}
    with pytest.raises(RelayWireError):
        unseal(other, envelope, request_aad(CFG.PING_PATH))
    with pytest.raises(RelayWireError):
        unseal(key, envelope, request_aad(CFG.STATE_PATH))  # a ping is not a state request
    tampered = {**envelope, "c": envelope["c"][:-2] + ("AA" if envelope["c"][-2:] != "AA" else "BB")}
    with pytest.raises(RelayWireError):
        unseal(key, tampered, request_aad(CFG.PING_PATH))
    answer, _ = seal(key, {"t_us": 2}, answer_aad(nonce))
    _, new_nonce = seal(key, {}, request_aad(CFG.PING_PATH))
    with pytest.raises(RelayWireError):
        unseal(key, answer, answer_aad(new_nonce))


@dataclass
class _SealedPostChannel:
    token: str = TOKEN
    kind: ChannelKind = "lan"
    label: str = "test channel"
    sealed_paths: list[str] = field(default_factory=list)

    def bind(self, _client: httpx.Client) -> _SealedPostChannel:
        return self

    def get(self, _path: str, _timeout: Timeout) -> httpx.Response:
        raise AssertionError("relay requested an unsealed GET")

    def post(self, _path: str, _body: bytes, _timeout: Timeout) -> httpx.Response:
        raise AssertionError("sealed relay frame used ordinary bearer-authenticated POST")

    def post_sealed(self, path: str, body: bytes, _timeout: Timeout) -> httpx.Response:
        key = relay_key(self.token)
        request, nonce = unseal(key, httpx.Response(200, content=body).json(), request_aad(path))
        self.sealed_paths.append(path)
        answer = {"t_us": 101.0} if path == CFG.PING_PATH else {"request": request}
        envelope, _ = seal(key, answer, answer_aad(nonce))
        return httpx.Response(200, json=envelope)


def test_exchange_and_ping_use_the_sealed_post_channel_operation() -> None:
    channel = _SealedPostChannel()
    ping_times = iter((100.0, 102.0))

    def now_us() -> float:
        return next(ping_times, 102.0)

    relay = LinkRelay(
        client=lambda: None,
        role=lambda: "following",
        peer=lambda: channel,
        verdict=lambda: ("multicast_blocked", "test"),
        enabled=lambda: True,
        wire_clock=WireClock(),
        now_us=now_us,
    )
    with httpx.Client() as client:
        answer = relay._exchange(client, channel, CFG.STATE_PATH, {"since": 7}, 1.0)
        relay._ping_once(client, channel)
    assert answer == {"request": {"since": 7}}
    assert channel.sealed_paths == [CFG.STATE_PATH, CFG.PING_PATH]
    assert relay.status()["counts"]["pings"] == 1


# ---------------------------------------------------------------- two relays in one process


@dataclass
class _Host:
    """The door's host protocol, paired, with no invite and no nearby service."""

    token: str
    relay: Any = None
    nearby: Any = None
    device_id: str = "dev"
    label: str = "test"

    def paired_token(self) -> str:
        return self.token

    def invite_open(self) -> bool:
        return False

    def pair_start(self, body: dict[str, Any], source_ip: str) -> dict[str, Any]:
        raise AssertionError("no pairing in this test")

    def pair_finish(self, body: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no pairing in this test")


@dataclass
class _Laptop:
    sim: SimCarabiner
    client: CarabinerClient
    door: Door
    host: _Host
    role: list[str]
    relay: LinkRelay | None = None
    verdict: list[str] = field(default_factory=lambda: ["multicast_blocked"])

    def close(self) -> None:
        if self.relay is not None:
            self.relay.stop()
        self.client.stop()
        self.door.stop()
        self.sim.close()


def _laptop(role: str, bpm: float, start_us: float, *, write_mode: str = "normal") -> _Laptop:
    sim = SimCarabiner(bpm=bpm, start_us=start_us, write_mode=write_mode)  # type: ignore[arg-type]
    client = CarabinerClient(port=sim.port)
    client.start()
    host = _Host(TOKEN)
    door = Door(host, lambda: "http://127.0.0.1:9", port=0, bind="127.0.0.1")
    return _Laptop(sim, client, door, host, [role])


#: The channel's own client is only for door_post's token lookup: the relay binds its loop clients.
_RELAY_CLIENT = httpx.Client()


def _start_relay(me: _Laptop, peer: _Laptop, wire: WireClock) -> None:
    me.relay = LinkRelay(
        client=lambda: me.client, role=lambda: me.role[0],
        peer=lambda: LanChannel(f"http://127.0.0.1:{peer.door.port}", TOKEN, _RELAY_CLIENT),
        verdict=lambda: (me.verdict[0], "test verdict"), enabled=lambda: True, wire_clock=wire,
    )
    me.host.relay = me.relay
    me.relay.start()


@pytest.fixture
def pair(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_Laptop, _Laptop]]:
    monkeypatch.setattr(CFG, "VERDICT_EVERY_S", 0.2)
    # 1 s skew buckets so the injected slope is known after about 6 s (production: 5 s buckets, 30 s).
    monkeypatch.setattr(relay_clock.CFG, "BUCKET_S", 1.0)
    now = link_now_us()
    leader = _laptop("on_air", 124.0, now - 37_123_456)
    follower = _laptop("following", 128.0, now - 11_000_000)
    _start_relay(leader, follower, WireClock(offset_us=1_234_567.0, skew_ppm=INJECTED_SKEW_PPM, ref_us=now))
    _start_relay(follower, leader, WireClock())
    yield leader, follower
    leader.close()
    follower.close()


def _phase_error_ms(leader: _Laptop, follower: _Laptop) -> float:
    """Ground truth: both sims are on this host's one Link clock."""
    now = link_now_us()
    a = beat_at_time(leader.sim.start_us, leader.sim.bpm, now)
    b = beat_at_time(follower.sim.start_us, follower.sim.bpm, now)
    return ((b - a + 2.0) % 4.0 - 2.0) * 60_000.0 / leader.sim.bpm


def _wait(
    what: str,
    probe: Callable[[], bool],
    budget_s: float,
    failure_state: Callable[[], object] | None = None,
) -> float:
    started = time.monotonic()
    while time.monotonic() - started < budget_s:
        if probe():
            return time.monotonic() - started
        time.sleep(0.01)
    state = "" if failure_state is None else f": {failure_state()}"
    raise AssertionError(f"timed out after {budget_s} s waiting for {what}{state}")


@pytest.mark.requirement("B2BRELAY-03")
@HOST_CLOCK_REQUIRED
def test_follower_locks_to_the_leader_and_a_tempo_change_crosses(pair: tuple[_Laptop, _Laptop]) -> None:
    leader, follower = pair
    follower_relay = follower.relay
    assert follower_relay is not None
    _wait("follower phase locked within 1 ms", lambda: abs(_phase_error_ms(leader, follower)) < 1.0, 10.0)
    # Until the slope is known the follower runs the WIRE tempo (200 ppm off) and forces keep the
    # phase; once known, the skew-converted tempo is the leader's physical tempo.
    _wait(
        "follower at the leader's physical tempo (skew converted)",
        lambda: abs(follower.sim.bpm - 124.0) < SKEW_BPM_TOL and abs(_phase_error_ms(leader, follower)) < 1.0,
        20.0,
        follower_relay.status,
    )
    status = follower_relay.status()
    assert (status["engaged"], status["mode"]) == (True, "follow"), status
    now = link_now_us()
    true_offset_ms = (leader.relay.wire.stamp(now) - now) / 1000.0  # type: ignore[union-attr]
    assert status["clock"]["offset_ms"] == pytest.approx(true_offset_ms, abs=1.0), (true_offset_ms, status["clock"])
    assert leader.relay.status()["mode"] == "lead"  # type: ignore[union-attr]
    assert (leader.relay.relay_peers(), follower_relay.relay_peers()) == (1, 1), (  # type: ignore[union-attr]
        "if an engaged relay with a live partner does not count it as the session's peer then the page holds"
    )
    errors = []
    for _ in range(20):
        errors.append(abs(_phase_error_ms(leader, follower)))
        time.sleep(0.05)
    assert max(errors) < CFG.LOCKED_MS, f"if the follower does not stay phase locked then broken: {errors}"

    leader.sim.set_tempo(126.0)
    took = _wait("follower at the new tempo", lambda: abs(follower.sim.bpm - 126.0) < TEMPO_MATCH_BPM, 0.5)
    print(f"[INFO] tempo crossed in {took * 1000:.0f} ms")


@pytest.mark.requirement("B2BRELAY-05")
@HOST_CLOCK_REQUIRED
def test_a_link_peer_stands_the_relay_down_and_nothing_is_written(pair: tuple[_Laptop, _Laptop]) -> None:
    leader, follower = pair
    _wait("follower locked", lambda: follower.relay.status()["sync"] == "locked", 10.0)  # type: ignore[union-attr]
    follower.sim.peers = 1
    follower.sim.push()
    _wait("relay stood down", lambda: follower.relay.status()["engaged"] is False, 2.0)  # type: ignore[union-attr]
    written = len(follower.sim.commands)
    leader.sim.set_tempo(130.0)
    time.sleep(1.0)
    writes = [c for c in follower.sim.commands[written:] if c != "status"]
    assert writes == [], f"if the relay writes Link while Link counts a peer then two paths run at once: {writes}"
    assert follower.sim.bpm != 130.0


@pytest.mark.requirement("B2BRELAY-05")
@HOST_CLOCK_REQUIRED
def test_the_write_guard_refuses_on_its_own_when_link_counts_a_peer() -> None:
    """The guard re-reads Link right before a write, independent of the engage rule (the race
    between a peer appearing and the next tick). Exercised with the relay forced engaged."""
    laptop = _laptop("following", 128.0, link_now_us())
    try:
        relay = LinkRelay(client=lambda: laptop.client, role=lambda: "following", peer=lambda: None,
                          verdict=lambda: ("multicast_blocked", ""), enabled=lambda: True, wire_clock=WireClock())
        relay.engaged = True
        leader = LeaderState(seq=1, role="on_air", engaged=True, joined=True, connected=True, peers=0, bpm=128.0,
                             start_us=0.0, received_at=time.monotonic())
        _wait("sim status read", lambda: laptop.client.snapshot().peers == 0, 2.0)
        assert relay._write_refusal(laptop.client, leader) is None, "control: all clear must allow the write"
        laptop.sim.peers = 1
        laptop.sim.push()
        _wait("peer seen", lambda: laptop.client.snapshot().peers == 1, 2.0)
        assert relay._write_refusal(laptop.client, leader) is not None, "if a write is allowed while Link counts a peer then broken"
        laptop.sim.peers = 0
        laptop.sim.push()
        _wait("peer gone", lambda: laptop.client.snapshot().peers == 0, 2.0)
        for bad in ({"peers": 1}, {"engaged": False}, {"role": "following"}):
            other = LeaderState(**{**leader.__dict__, **bad})
            assert relay._write_refusal(laptop.client, other) is not None, f"if the guard allows a leader with {bad} then broken"
    finally:
        laptop.close()


@pytest.mark.requirement("B2BRELAY-07")
@HOST_CLOCK_REQUIRED
def test_a_partition_holds_as_follow_with_no_relay_peer_and_writes_nothing(
    pair: tuple[_Laptop, _Laptop], monkeypatch: pytest.MonkeyPatch
) -> None:
    leader, follower = pair
    _wait("follower locked", lambda: follower.relay.status()["sync"] == "locked", 10.0)  # type: ignore[union-attr]
    leader.door.stop()  # the partner goes silent: no ping, no state
    _wait("holding", lambda: follower.relay.status()["sync"] == "holding", CFG.STALE_AFTER_S + 3.0)  # type: ignore[union-attr]
    status = follower.relay.status()  # type: ignore[union-attr]
    assert (status["engaged"], status["mode"], status["relay_peers"]) == (True, "follow", 0), status
    assert follower.relay.relay_peers() == 0, "if a silent partner still counts as a peer then a partition never holds"  # type: ignore[union-attr]
    written = [c for c in follower.sim.commands if c != "status"]
    time.sleep(1.0)
    assert [c for c in follower.sim.commands if c != "status"] == written, "if the follower writes Link on stale state then broken"


# ---------------------------------------------------------------- alignment acknowledgement


def _clock_estimate(offset_us: float = 0.0, skew_ppm: float = 0.0) -> ClockEstimate:
    return ClockEstimate(
        offset_us=offset_us,
        skew_ppm=skew_ppm,
        skew_ready=True,
        skew_clamped=False,
        rtt_min_us=1_000.0,
        rtt_p50_us=2_000.0,
        samples=8,
        used=8,
    )


def _alignment_relay(write_mode: str = "normal") -> tuple[LinkRelay, CarabinerClient, LeaderState, SimCarabiner]:
    """Engaged follower with a leader that forces a tempo write on the next align."""
    now = link_now_us()
    sim = SimCarabiner(bpm=128.0, start_us=now - 11_000_000, write_mode=write_mode)  # type: ignore[arg-type]
    client = CarabinerClient(port=sim.port)
    client.start()
    leader = LeaderState(
        seq=1,
        role="on_air",
        engaged=True,
        joined=True,
        connected=True,
        peers=0,
        bpm=124.0,
        start_us=now + 1_234_567.0 - 37_123_456,
        received_at=time.monotonic(),
    )
    relay = LinkRelay(
        client=lambda: client,
        role=lambda: "following",
        peer=lambda: None,
        verdict=lambda: ("multicast_blocked", "test"),
        enabled=lambda: True,
        wire_clock=WireClock(offset_us=1_234_567.0, skew_ppm=0.0, ref_us=now),
    )
    relay.engaged = True
    relay._leader = leader
    relay._estimator.estimate = lambda _now: _clock_estimate(offset_us=1_234_567.0)  # type: ignore[method-assign]
    try:
        _wait(
            "carabiner session snapshot",
            lambda: client.snapshot().connected
            and client.snapshot().bpm is not None
            and client.snapshot().start_us is not None,
            2.0,
        )
    except AssertionError:
        client.stop()
        sim.close()
        raise
    return relay, client, leader, sim


@pytest.mark.requirement("RBPERFORMANCE-13262")
def test_status_sync_not_locked_after_carabiner_rejects_alignment() -> None:
    """[if] Carabiner rejects align [then] sync not locked and outcome rejected, [else stop]."""
    relay, client, leader, sim = _alignment_relay("reject")
    try:
        relay._align(client, leader)
        status = relay.status()
        assert status["sync"] != "locked"
        assert status["last_alignment"]["outcome"] == "rejected"
        assert status["counts"]["refused_writes"] == 1
        assert status["last_error"]
    finally:
        client.stop()
        sim.close()


@pytest.mark.requirement("RBPERFORMANCE-13262")
def test_status_sync_not_locked_after_carabiner_alignment_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Carabiner silent on align [then] sync not locked and outcome timeout, [else stop]."""
    monkeypatch.setattr(carabiner_mod, "REPLY_TIMEOUT_S", 0.05)
    relay, client, leader, sim = _alignment_relay("silent")
    try:
        relay._align(client, leader)
        status = relay.status()
        assert status["sync"] != "locked"
        assert status["last_alignment"]["outcome"] == "timeout"
        assert status["counts"]["refused_writes"] == 1
        assert status["last_error"]
    finally:
        client.stop()
        sim.close()


@pytest.mark.requirement("RBPERFORMANCE-13262")
@HOST_CLOCK_REQUIRED
def test_status_acknowledged_after_successful_alignment_write() -> None:
    """[if] Carabiner acks align [then] outcome acknowledged and tempo_sets increments, [else stop]."""
    relay, client, leader, sim = _alignment_relay("normal")
    try:
        relay._align(client, leader)
        status = relay.status()
        assert status["last_alignment"]["outcome"] == "acknowledged"
        assert status["counts"]["tempo_sets"] == 1
        assert status["last_alignment"]["error"] is None
        assert status["mode"] == "follow"
        with relay._lock:
            plan = relay._plan
            assert isinstance(plan, AlignPlan)
            relay._plan = replace(plan, phase_error_ms=1.0)
        locked = relay.status()
        assert locked["sync"] == "locked"
        assert abs(locked["phase_error_ms"]) <= CFG.LOCKED_MS
    finally:
        client.stop()
        sim.close()
