"""B2BMCAST-01/02/04: the multicast-blocked diagnosis as a pure state machine.

Every input is a real dataclass from ``apps.ableton_link.multicast_diagnosis``; the peer probe
gets real ``httpx.Response`` objects from an injected ``http_get``, and ``NearbyHold`` an
injected clock. No sockets.

- [if] a Link state the module declares cannot be reached from real evidence [then] broken
- [if] multicast_blocked is named before the hold, or not at exactly the hold [then] broken
- [if] the hold ignores the partner's own connected_for_s (min of both sides) [then] broken
- [if] an unmeasured duration or an unknown peer report is named blocked [then] broken
- [if] a Nearby scan that lists the partner does not reset the hold [then] broken
- [if] the panel copy differs from the agreed text for link, nearby, both, or Link-off [then] broken
- [if] a probe answer is classified into the wrong report kind, or the bearer is not sent [then] broken
- [if] the partner is silent on Link or Nearby for the hold [then] it reads multicast_blocked, never sooner, [else stop]
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from apps.ableton_link import multicast_diagnosis as md
from apps.ableton_link.multicast_diagnosis import (
    FIXES,
    LinkFacts,
    LocalLink,
    NearbyEvidence,
    NearbyHold,
    PeerChannel,
    PeerReport,
    ReachEvidence,
    Verdict,
    build_message,
    diagnose_link,
    diagnosis_answer,
    parse_peer_report,
    probe_peer_channel,
)

pytestmark = [
    pytest.mark.requirement("B2BMCAST-01"),
    pytest.mark.requirement("B2BMCAST-02"),
    pytest.mark.requirement("B2BMCAST-04"),
]

HOLD = 10.0
URL = "http://192.0.2.7:40001"

# ----------------------------------------------------------------- builders


def _peer(enabled: bool = True, connected: bool = True, connected_for_s: float | None = 60.0,
          label: str | None = "Peter", listed: bool = True) -> PeerReport:
    return PeerReport(label=label, link=LinkFacts(enabled, connected, 0, connected_for_s), nearby_listed=listed)


def _up(peer: PeerReport | None = None, report: md.PeerReportKind | None = "known") -> PeerChannel:
    return PeerChannel("up", URL, "paired", 200, 3.2, report, "answered", _peer() if peer is None and report == "known" else peer)


DOWN = PeerChannel("down", URL, "paired", None, None, None, "The other laptop did not answer.")
NONE = PeerChannel("none", None, "unset", None, None, None, "Not paired and no PEER URL set.")


def _local(peers: int | None = 0, zero_for: float | None = 60.0, joined: bool = True, connected: bool = True,
           error: str | None = None) -> LocalLink:
    return LocalLink(joined=joined, connected=connected, peers=peers, zero_peers_for_s=zero_for, error=error)


REACH_NONE = ReachEvidence("none_heard", 0, "no Link packets from other hosts")
REACH_HEARD = ReachEvidence("heard", 2, "2 nodes heard")
REACH_UNKNOWN = ReachEvidence("unknown", None, "listener warming up")


# ----------------------------------------------------------------- Link


def test_peers_above_zero_is_ok() -> None:
    v = diagnose_link(_up(), _local(peers=2, zero_for=None), REACH_NONE, HOLD)
    assert v.state == "ok", f"if 2 local peers is not ok then broken: {v}"


def test_waiting_carries_for_and_remaining() -> None:
    v = diagnose_link(_up(), _local(zero_for=3.0), REACH_NONE, HOLD)
    assert v.state == "waiting", f"if 3 s of 0 peers is not waiting then broken: {v}"
    assert v.for_s == 3.0 and v.remaining_s == 7.0, f"if waiting does not carry for_s 3.0 and remaining_s 7.0 then broken: {v}"


def test_multicast_blocked_at_exactly_the_hold_and_not_just_before() -> None:
    before = diagnose_link(_up(), _local(zero_for=9.9), REACH_NONE, HOLD)
    assert before.state == "waiting" and before.remaining_s == 0.1, f"if 9.9 s is not still waiting then broken: {before}"
    at = diagnose_link(_up(), _local(zero_for=HOLD), REACH_NONE, HOLD)
    assert at.state == "multicast_blocked", f"if exactly the hold is not multicast_blocked then broken: {at}"
    assert at.for_s == HOLD and at.remaining_s is None, f"if a blocked verdict carries remaining_s then broken: {at}"
    assert "could not measure" not in at.reason, f"if a measured reach still says unmeasured then broken: {at.reason}"
    assert "No other host's Link packets were heard here." in at.reason, f"if a measured empty reach is not stated then broken: {at.reason}"


def test_hold_is_min_of_local_zero_streak_and_partner_connected_for() -> None:
    # Local has had 0 peers for a minute, but the partner only joined 4 s ago: still waiting.
    young_partner = _up(_peer(connected_for_s=4.0))
    v = diagnose_link(young_partner, _local(zero_for=60.0), REACH_NONE, HOLD)
    assert v.state == "waiting" and v.for_s == 4.0, f"if the partner's connected_for_s does not bound the hold then broken: {v}"
    # Symmetric: partner joined long ago, local streak is young.
    v = diagnose_link(_up(_peer(connected_for_s=600.0)), _local(zero_for=2.5), REACH_NONE, HOLD)
    assert v.state == "waiting" and v.for_s == 2.5, f"if the local zero streak does not bound the hold then broken: {v}"


def test_local_network_blocked_when_reach_hears_other_hosts() -> None:
    v = diagnose_link(_up(), _local(zero_for=30.0), REACH_HEARD, HOLD)
    assert v.state == "local_network_blocked", f"if heard packets with 0 peers is not a local block then broken: {v}"
    assert "Local Network" in v.reason and "2 other host" in v.reason, f"if the reason does not name the macOS block then broken: {v.reason}"


def test_reach_unknown_still_allows_blocked_and_says_so() -> None:
    v = diagnose_link(_up(), _local(zero_for=30.0), REACH_UNKNOWN, HOLD)
    assert v.state == "multicast_blocked", f"if reach unknown prevents multicast_blocked then broken: {v}"
    assert "could not measure" in v.reason and "listener warming up" in v.reason, (
        f"if an unmeasured reach is not stated in the reason then broken: {v.reason}"
    )
    assert "were heard" not in v.reason, f"if an unmeasured reach claims nothing was heard then broken: {v.reason}"


@pytest.mark.parametrize(
    ("channel", "fragment"),
    [
        (_up(_peer(enabled=False)), "has not joined Link"),
        (_up(_peer(connected=False)), "Carabiner is not connected"),
        (PeerChannel("up", URL, "paired", 403, 2.0, "link_flag_off", "Link is turned off in its build (flag link.tempo_sync)."),
         "link.tempo_sync"),
    ],
    ids=["remote-not-enabled", "remote-not-connected", "remote-flag-off"],
)
def test_link_off_remote(channel: PeerChannel, fragment: str) -> None:
    v = diagnose_link(channel, _local(zero_for=600.0), REACH_NONE, HOLD)
    assert v.state == "link_off_remote", f"if a partner with Link off is not link_off_remote then broken: {v}"
    assert fragment in v.reason, f"if the reason does not say why ({fragment!r}) then broken: {v.reason}"


@pytest.mark.parametrize(
    "local",
    [_local(joined=False, connected=False, peers=None, zero_for=None),
     _local(connected=False, peers=None, zero_for=None, error="Carabiner closed the connection")],
    ids=["not-joined", "carabiner-down"],
)
def test_link_off_local_wins_over_everything(local: LocalLink) -> None:
    v = diagnose_link(_up(_peer(enabled=False)), local, REACH_HEARD, HOLD)
    assert v.state == "link_off_local", f"if this laptop's Link being off is not link_off_local then broken: {v}"


def test_no_peer_and_peer_unreachable() -> None:
    assert diagnose_link(NONE, _local(), REACH_NONE, HOLD).state == "no_peer", "if no partner is not no_peer then broken"
    v = diagnose_link(DOWN, _local(), REACH_NONE, HOLD)
    assert v.state == "peer_unreachable", f"if a dead partner is not peer_unreachable then broken: {v}"
    assert "not a multicast problem" in v.reason, f"if unreachable is blamed on multicast then broken: {v.reason}"


@pytest.mark.parametrize("report", ["refused", "old_peer", "bad_answer"])
def test_unknown_peer_report_is_unknown_never_blocked(report: md.PeerReportKind) -> None:
    v = diagnose_link(_up(None, report=report), _local(zero_for=600.0), REACH_NONE, HOLD)
    assert v.state == "unknown", f"if a {report} partner (Link state unknown) is judged then broken: {v}"


@pytest.mark.parametrize(
    ("local", "peer"),
    [(_local(zero_for=None), _peer()), (_local(zero_for=600.0), _peer(connected_for_s=None))],
    ids=["local-unmeasured", "partner-unmeasured"],
)
def test_unmeasured_durations_are_unknown(local: LocalLink, peer: PeerReport) -> None:
    v = diagnose_link(_up(peer), local, REACH_NONE, HOLD)
    assert v.state == "unknown", f"if an unmeasured 0-peer duration is judged then broken: {v}"


def test_no_peer_count_yet_is_unknown() -> None:
    v = diagnose_link(_up(), _local(peers=None, zero_for=None), REACH_NONE, HOLD)
    assert v.state == "unknown", f"if no Carabiner peer count is judged then broken: {v}"


# ----------------------------------------------------------------- Nearby


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _scan(at: float | None, listed: bool | None, error: str | None = None, paired: bool = True,
          running: bool = True) -> NearbyEvidence:
    return NearbyEvidence(paired=paired, running=running, scanned_at=at, scan_error=error, partner_listed=listed)


def test_nearby_absent_across_scans_waits_then_blocks() -> None:
    clock = _Clock(1000.0)
    hold = NearbyHold(hold_s=HOLD, clock=clock)
    v = hold.observe(_up(), _scan(1000.0, False))
    assert v.state == "waiting" and v.for_s == 0.0 and v.remaining_s == HOLD, f"if the first absent scan is not waiting 0 s then broken: {v}"
    clock.now = 1005.0
    v = hold.observe(_up(), _scan(1004.5, False))
    assert v.state == "waiting" and v.for_s == 5.0, f"if a later absent scan restarts the hold then broken: {v}"
    clock.now = 1010.0
    v = hold.observe(_up(), _scan(1009.0, False))
    assert v.state == "multicast_blocked" and v.for_s == HOLD, f"if absent for the hold is not multicast_blocked then broken: {v}"


def test_nearby_listing_resets_the_hold() -> None:
    clock = _Clock(1000.0)
    hold = NearbyHold(hold_s=HOLD, clock=clock)
    hold.observe(_up(), _scan(1000.0, False))
    clock.now = 1008.0
    assert hold.observe(_up(), _scan(1008.0, True)).state == "ok", "if a scan listing the partner is not ok then broken"
    clock.now = 1012.0
    v = hold.observe(_up(), _scan(1012.0, False))
    assert v.state == "waiting" and v.for_s == 0.0, f"if the hold survives a scan that listed the partner then broken: {v}"


@pytest.mark.parametrize(
    ("evidence", "state"),
    [
        (_scan(1000.0, None, error="dns-sd exited 1"), "unknown"),
        (_scan(None, None), "unknown"),
        (_scan(1000.0, False, paired=False), "not_paired"),
        (_scan(1000.0, False, running=False), "nearby_off_local"),
    ],
    ids=["scan-error", "no-scan", "not-paired", "nearby-off-local"],
)
def test_nearby_settled_states(evidence: NearbyEvidence, state: str) -> None:
    v = NearbyHold(hold_s=HOLD, clock=_Clock()).observe(_up(), evidence)
    assert v.state == state, f"if {evidence} is not {state} then broken: {v}"


def test_nearby_remote_not_listed_and_channel_states() -> None:
    hold = NearbyHold(hold_s=HOLD, clock=_Clock())
    assert hold.observe(_up(_peer(listed=False)), _scan(1000.0, False)).state == "nearby_off_remote", (
        "if a partner that does not list itself is not nearby_off_remote then broken"
    )
    assert hold.observe(DOWN, _scan(1000.0, False)).state == "peer_unreachable", "if a dead partner is not peer_unreachable then broken"
    assert hold.observe(NONE, _scan(1000.0, False)).state == "unknown", "if paired-but-no-url is not unknown then broken"


def test_nearby_scan_error_resets_the_hold() -> None:
    clock = _Clock(1000.0)
    hold = NearbyHold(hold_s=HOLD, clock=clock)
    hold.observe(_up(), _scan(1000.0, False))
    clock.now = 1009.0
    assert hold.observe(_up(), _scan(1009.0, None, error="timeout")).state == "unknown"
    clock.now = 1011.0
    v = hold.observe(_up(), _scan(1011.0, False))
    assert v.state == "waiting", f"if an unknown scan does not restart the hold then broken: {v}"


# ----------------------------------------------------------------- the message (B2BMCAST-04)

BLOCKED = Verdict("multicast_blocked", "link evidence", 12.0)
NEARBY_BLOCKED = Verdict("multicast_blocked", "nearby evidence", 12.0)
WAITING = Verdict("waiting", "w", 2.0, 8.0)


@pytest.mark.parametrize(
    ("link", "nearby", "what", "title"),
    [
        (BLOCKED, WAITING, "Ableton Link", "link evidence"),
        (WAITING, NEARBY_BLOCKED, "Nearby", "nearby evidence"),
        (BLOCKED, NEARBY_BLOCKED, "Ableton Link and Nearby", "link evidence nearby evidence"),
    ],
    ids=["link-only", "nearby-only", "both"],
)
def test_blocked_message_copy(link: Verdict, nearby: Verdict, what: str, title: str) -> None:
    message = build_message(link, nearby, _up())
    assert message == {
        "code": "multicast_blocked", "severity": "warn", "headline": "This network is blocking Link",
        "text": (
            "Your laptops can reach each other, but this network does not pass the broadcast traffic that "
            f"{what} uses to find Peter. Venue, hotel and guest Wi-Fi often block it."
        ),
        "fixes": [
            "Connect the two laptops with an Ethernet or Thunderbolt cable.",
            "Or share a phone hotspot and join both laptops to it.",
            "Or bring a travel router and join both laptops to it.",
        ],
        "title": title,
    }, f"if the blocked copy for {what} drifts then broken: {message}"
    assert message["fixes"] is not FIXES, "if the message hands out the module's own tuple then broken"


def test_link_off_remote_message_and_unnamed_partner() -> None:
    off = Verdict("link_off_remote", "The other laptop answers but has not joined Link (its LINK is off).")
    message = build_message(off, WAITING, _up())
    assert message == {
        "code": "link_off_remote", "severity": "info", "headline": "Peter's Link is off",
        "text": "Your laptops can reach each other. Ask Peter to press LINK.", "fixes": [], "title": off.reason,
    }, f"if the Link-off copy drifts then broken: {message}"
    unnamed = build_message(off, WAITING, _up(_peer(label=None)))
    assert unnamed is not None
    assert unnamed["headline"] == "The other laptop's Link is off", f"if an unnamed partner headline is not sentence case then broken: {unnamed}"
    assert unnamed["text"] == "Your laptops can reach each other. Ask the other laptop to press LINK.", unnamed
    blocked = build_message(BLOCKED, WAITING, DOWN)
    assert blocked is not None and "to find the other laptop." in blocked["text"], (
        f"if no peer label is not 'the other laptop' then broken: {blocked}"
    )


@pytest.mark.parametrize(
    "link", [Verdict("ok", "r"), WAITING, Verdict("unknown", "r"), Verdict("no_peer", "r"), Verdict("link_off_local", "r"),
             Verdict("local_network_blocked", "r"), Verdict("peer_unreachable", "r")],
    ids=lambda v: v.state,
)
def test_no_message_otherwise(link: Verdict) -> None:
    assert build_message(link, Verdict("unknown", "r"), _up()) is None, f"if {link.state} draws a panel message then broken"


def test_diagnosis_answer_shape_omits_nested_peer_from_channel() -> None:
    answer = diagnosis_answer(_up(), BLOCKED, WAITING, HOLD)
    assert set(answer) == {"hold_s", "peer_channel", "peer", "link", "nearby", "message"}
    assert "peer" not in answer["peer_channel"], "if the channel repeats the peer report then broken"
    assert answer["peer"] == {"label": "Peter", "link": {"enabled": True, "connected": True, "peers": 0, "connected_for_s": 60.0},
                              "nearby": {"listed": True}}
    assert answer["message"]["code"] == "multicast_blocked"


# ----------------------------------------------------------------- the peer probe


def _self_body(**link: Any) -> dict[str, Any]:
    base = {"enabled": True, "connected": True, "peers": 0, "connected_for_s": 42.5}
    return {"label": "Peter", "link": {**base, **link}, "nearby": {"listed": True}}


class _Get:
    """An ``http_get`` that answers one real ``httpx.Response`` and records the call."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _resp(status: int, *, body: Any = None, text: str | None = None) -> httpx.Response:
    request = httpx.Request("GET", f"{URL}{md.CFG.SELF_ROUTE}")
    if text is not None:
        return httpx.Response(status, text=text, request=request)
    return httpx.Response(status, json=body, request=request)


def test_probe_200_good_report_sends_bearer() -> None:
    get = _Get(_resp(200, body=_self_body()))
    channel = probe_peer_channel(URL, "s3cret-token-0000", "paired", http_get=get)
    assert channel.state == "up" and channel.report == "known" and channel.http_status == 200, (
        f"if a good self report is not known then broken: {channel}"
    )
    assert channel.peer == PeerReport("Peter", LinkFacts(True, True, 0, 42.5), True), channel.peer
    assert channel.rtt_ms is not None and channel.rtt_ms >= 0
    url, kwargs = get.calls[0]
    assert url == f"{URL}/api/v1/b2b/multicast/self", f"if the probe asks another route then broken: {url}"
    assert kwargs["headers"] == {"Authorization": "Bearer s3cret-token-0000"}, f"if the bearer is not sent then broken: {kwargs}"
    assert kwargs["timeout"] == md.CFG.PEER_TIMEOUT_S


def test_probe_without_token_sends_no_authorization() -> None:
    get = _Get(_resp(200, body=_self_body()))
    probe_peer_channel(URL, None, "env", http_get=get)
    assert get.calls[0][1]["headers"] == {}, "if a None token sends 'Bearer None' then broken"


@pytest.mark.parametrize(
    "body",
    [_self_body(peers=-1), _self_body(peers=True), _self_body(enabled="yes"), _self_body(connected_for_s=float("inf")),
     {"label": "x", "link": {}}, [1, 2]],
    ids=["negative-peers", "bool-peers", "string-enabled", "inf-duration", "missing-nearby", "list"],
)
def test_probe_200_malformed_is_bad_answer(body: Any) -> None:
    # Sent as text: json.dumps writes inf as ``Infinity``, which a lenient peer could emit and json.loads reads back.
    channel = probe_peer_channel(URL, "t", "paired", http_get=_Get(_resp(200, text=json.dumps(body))))
    assert channel.state == "up" and channel.report == "bad_answer" and channel.peer is None, (
        f"if a malformed self report is not bad_answer then broken: {channel}"
    )


def test_probe_200_non_json_is_bad_answer() -> None:
    channel = probe_peer_channel(URL, "t", "paired", http_get=_Get(_resp(200, text="<html>captive portal</html>")))
    assert channel.report == "bad_answer" and "did not parse" in channel.reason, f"if a non-JSON body is not bad_answer then broken: {channel}"


@pytest.mark.parametrize(
    ("response", "report"),
    [
        (_resp(401, body={"detail": "not paired, or wrong pairing token"}), "refused"),
        (_resp(403, body={"detail": "Link is off: flag link.tempo_sync is disabled"}), "link_flag_off"),
        (_resp(403, body={"detail": "forbidden"}), "bad_answer"),
        (_resp(404, body={"detail": "Not Found"}), "old_peer"),
        (_resp(502, body={"detail": "engine unreachable"}), "bad_answer"),
    ],
    ids=["401", "403-flag", "403-other", "404", "502"],
)
def test_probe_status_classification(response: httpx.Response, report: str) -> None:
    channel = probe_peer_channel(URL, "t", "paired", http_get=_Get(response))
    assert channel.state == "up" and channel.report == report, f"if HTTP {response.status_code} is not {report} then broken: {channel}"
    assert channel.http_status == response.status_code and channel.peer is None


def test_probe_connect_error_is_down_and_no_url_is_none() -> None:
    error = httpx.ConnectError("[Errno 111] Connection refused")
    channel = probe_peer_channel(URL, "t", "paired", http_get=_Get(error))
    assert channel.state == "down" and channel.report is None and "ConnectError" in channel.reason, (
        f"if a refused connect is not down then broken: {channel}"
    )
    get = _Get(AssertionError("must not be called"))
    none = probe_peer_channel(None, "t", "unset", http_get=get)
    assert none.state == "none" and none.url is None and get.calls == [], f"if no url is probed anyway then broken: {none}"


def test_parse_peer_report_blank_label_is_none_and_int_duration_is_float() -> None:
    peer = parse_peer_report({"label": "", "link": {"enabled": False, "connected": False, "peers": None, "connected_for_s": 3},
                              "nearby": {"listed": False}})
    assert peer.label is None and peer.link.connected_for_s == 3.0 and isinstance(peer.link.connected_for_s, float)


@pytest.mark.requirement("B2BNEAR-06")
def test_nearby_partial_scan_without_the_partner_is_unknown_never_blocked() -> None:
    """A scan that ran out of time before every instance answered cannot say the partner is absent."""
    evidence = NearbyEvidence(paired=True, running=True, scanned_at=1000.0, scan_error=None, partner_listed=None,
                              scan_note="1 of 2 Open DJs found did not answer in time")
    v = NearbyHold(hold_s=HOLD, clock=_Clock(1000.0)).observe(_up(), evidence)
    assert v.state == "unknown" and "incomplete" in v.reason and "1 of 2" in v.reason, (
        f"if a partial scan reads as the partner missing then broken: {v}"
    )
