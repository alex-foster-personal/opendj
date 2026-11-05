"""B2BMCAST-01..03: say so when the network blocks the multicast Link and Nearby need.

Ableton Link (through Carabiner) finds peers by UDP multicast, and Nearby finds the partner
by Bonjour, which is multicast too. Venue, hotel and guest Wi-Fi often isolate clients, and
Tailscale carries no multicast (measured Sat 10 Oct 2026: 106 packets sent, 0 received), so
two laptops can reach each other by plain HTTP while Link counts 0 peers forever. Before this
module the panel just showed nothing happening.

The diagnosis is passive, built only from evidence already measured:

- the PEER CHANNEL: one GET of the partner's ``/api/v1/b2b/multicast/self`` through its B2B
  door (bearer token). Any HTTP answer proves unicast works; the answer's body is the
  partner's own report (Link joined, Carabiner connected, how long, Nearby listed), which is
  what separates "the network blocks multicast" from "Link is off over there";
- Carabiner's peer count and how long it has read 0 WITHOUT A BREAK, measured by the
  Carabiner reader thread's own status polling (``LinkSnapshot.zero_peers_for_s``), so the
  hold does not depend on who polls this route or how often;
- the receive-only Link reach listener (``link_reach``): if it hears another host's Link
  packets while Carabiner counts 0, multicast is arriving and the cause is a local block
  (macOS Local Network privacy), not the network;
- for Nearby, the last Bonjour scans: whether the remembered partner was listed.

Every input that cannot be measured makes the verdict ``unknown`` with the reason. Unknown
is never reported as fine, and an absence of evidence is never reported as a block.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

import httpx


class CFG:
    #: How long Carabiner must count 0 Link peers (and the partner must have been joined)
    #: before the network is named as the cause. Link drops a silent peer after its 5 s alive
    #: ttl and re-announces several times a second, so a reachable peer shows within about a
    #: second of both joining. 10 s = Link's own 5 s ttl plus 5 s margin: a Wi-Fi roam or a
    #: partner that is still joining reads ``waiting``, a blocked network is named within 10 s.
    HOLD_S: float = 10.0
    #: The partner self-report GET; the panel polls every 5 s, so keep a dead peer cheap.
    PEER_TIMEOUT_S: float = 1.5
    SELF_ROUTE: str = "/api/v1/b2b/multicast/self"


FIXES: tuple[str, ...] = (
    "Connect the two laptops with an Ethernet or Thunderbolt cable.",
    "Or share a phone hotspot and join both laptops to it.",
    "Or bring a travel router and join both laptops to it.",
)
BLOCKED_HEADLINE = "This network is blocking Link"
BLOCKED_TEXT = (
    "Your laptops can reach each other, but this network does not pass the broadcast traffic that "
    "{what} uses to find {label}. Venue, hotel and guest Wi-Fi often block it."
)
LINK_OFF_HEADLINE = "{label}'s Link is off"
LINK_OFF_TEXT = "Your laptops can reach each other. Ask {label} to press LINK."
UNNAMED_PARTNER = "the other laptop"

ChannelState = Literal["up", "down", "none"]
PeerReportKind = Literal["known", "refused", "old_peer", "link_flag_off", "bad_answer"]
LinkState = Literal[
    "ok", "waiting", "multicast_blocked", "local_network_blocked", "link_off_remote",
    "link_off_local", "no_peer", "peer_unreachable", "unknown",
]
NearbyState = Literal[
    "ok", "waiting", "multicast_blocked", "nearby_off_remote", "nearby_off_local",
    "not_paired", "peer_unreachable", "unknown",
]
ReachState = Literal["heard", "none_heard", "unknown"]


# ---------------------------------------------------------------- evidence


@dataclass(frozen=True)
class LinkFacts:
    """One laptop's Link as it reports itself (this one, or the partner over the door)."""

    enabled: bool
    connected: bool
    peers: int | None
    connected_for_s: float | None


@dataclass(frozen=True)
class PeerReport:
    label: str | None
    link: LinkFacts
    nearby_listed: bool


@dataclass(frozen=True)
class PeerChannel:
    state: ChannelState
    url: str | None
    source: str
    http_status: int | None
    rtt_ms: float | None
    report: PeerReportKind | None
    reason: str
    peer: PeerReport | None = None


@dataclass(frozen=True)
class LocalLink:
    """This laptop's Carabiner. ``joined`` False = LINK not pressed (no client)."""

    joined: bool
    connected: bool
    peers: int | None
    zero_peers_for_s: float | None
    error: str | None


@dataclass(frozen=True)
class ReachEvidence:
    state: ReachState
    #: Link nodes heard from OTHER hosts (this laptop's own apps excluded); None = not measured.
    other_hosts_heard: int | None
    reason: str


@dataclass(frozen=True)
class NearbyEvidence:
    paired: bool
    running: bool
    #: Monotonic time the last Bonjour scan finished; None = no scan has finished.
    scanned_at: float | None
    scan_error: str | None
    #: Whether the last finished scan listed the remembered partner; None before any scan, or
    #: when a partial scan (B2BNEAR-06) missed it.
    partner_listed: bool | None
    #: Why the last scan may be incomplete; None when it heard everyone in time.
    scan_note: str | None = None


@dataclass(frozen=True)
class Verdict:
    state: str
    reason: str
    #: How long the failing condition has held (0 Link peers / partner not listed).
    for_s: float | None = None
    #: ``hold_s - for_s`` while ``waiting``; None otherwise.
    remaining_s: float | None = None


# ---------------------------------------------------------------- the partner's self report


def parse_peer_report(payload: object) -> PeerReport:
    """The partner's ``/b2b/multicast/self`` body -> a report; TypeError on any other shape."""
    report = _object(payload, "report")
    link, nearby = _object(report.get("link"), "link"), _object(report.get("nearby"), "nearby")
    label = report.get("label")
    return PeerReport(
        label=label if isinstance(label, str) and label else None,
        link=LinkFacts(
            _bool(link, "enabled"), _bool(link, "connected"), _count_or_none(link.get("peers")),
            _seconds_or_none(link.get("connected_for_s")),
        ),
        nearby_listed=_bool(nearby, "listed"),
    )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} is not an object: {type(value).__name__}")
    return value


def _bool(obj: dict[str, Any], key: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean, got {value!r}")
    return value


def _count_or_none(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError(f"link.peers must be a count or null, got {value!r}")
    return value


def _seconds_or_none(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise TypeError(f"link.connected_for_s must be seconds or null, got {value!r}")
    return float(value)


HttpGet = Callable[..., httpx.Response]


def probe_peer_channel(url: str | None, token: str | None, source: str, http_get: HttpGet = httpx.get) -> PeerChannel:
    """One GET of the partner's self report. Any HTTP answer means unicast works (``up``)."""
    if url is None:
        return PeerChannel("none", None, source, None, None, None, "Not paired and no PEER URL set: no partner to compare with.")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    started = time.perf_counter()
    try:
        response = http_get(f"{url}{CFG.SELF_ROUTE}", headers=headers, timeout=CFG.PEER_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return PeerChannel(
            "down", url, source, None, None, None,
            f"The other laptop at {url} did not answer ({type(exc).__name__}), so not even plain network traffic gets through.",
        )
    rtt = round((time.perf_counter() - started) * 1000.0, 1)
    status = response.status_code

    def up(report: PeerReportKind, reason: str, peer: PeerReport | None = None) -> PeerChannel:
        return PeerChannel("up", url, source, status, rtt, report, reason, peer)

    if status == 200:
        try:
            peer = parse_peer_report(response.json())
        except (TypeError, ValueError) as exc:  # ValueError: a body that is not JSON
            return up("bad_answer", f"The other laptop answered in {rtt} ms but its report did not parse ({exc}).")
        return up("known", f"The other laptop answered in {rtt} ms with its own Link and Nearby state.", peer)
    if status == 401:
        return up("refused", f"The other laptop answered in {rtt} ms but refused this laptop's pairing token (401).")
    if status == 403 and "link.tempo_sync" in response.text:
        return up("link_flag_off", f"The other laptop answered in {rtt} ms: Link is turned off in its build (flag link.tempo_sync).")
    if status == 404:
        return up("old_peer", f"The other laptop answered in {rtt} ms but its Open DJ is older and does not report its Link state.")
    return up("bad_answer", f"The other laptop answered {status} in {rtt} ms, not its Link state.")


# ---------------------------------------------------------------- Link


def diagnose_link(channel: PeerChannel, local: LocalLink, reach: ReachEvidence, hold_s: float = CFG.HOLD_S) -> Verdict:
    """B2BMCAST-01. Stateless: the 0-peer duration is Carabiner's own continuous measure."""
    settled = _settled_by_local_link(local) or _settled_by_partner(channel)
    if settled is not None:
        return settled
    return _judge_zero_peers(channel, local, reach, hold_s)


def _settled_by_local_link(local: LocalLink) -> Verdict | None:
    """A verdict from this laptop's Link alone; None when it counts 0 peers and must look further."""
    if not local.joined:
        return Verdict("link_off_local", "This laptop has not joined Link (LINK is off).")
    if not local.connected:
        return Verdict("link_off_local", f"This laptop's Carabiner is not connected: {local.error or 'no status line yet'}.")
    if local.peers is None:
        return Verdict("unknown", "Carabiner has not reported a peer count yet.")
    if local.peers > 0:
        return Verdict("ok", f"Carabiner counts {local.peers} Link peer(s).")
    return None


def _settled_by_partner(channel: PeerChannel) -> Verdict | None:
    """With 0 local peers: a verdict when the partner is absent, unreachable, silent or Link-off."""
    if channel.state == "none":
        return Verdict("no_peer", channel.reason)
    if channel.state == "down":
        return Verdict("peer_unreachable", f"{channel.reason} That is not a multicast problem; check both laptops are on one network.")
    if channel.report == "link_flag_off":
        return Verdict("link_off_remote", channel.reason)
    if channel.peer is None:
        return Verdict("unknown", f"{channel.reason} Whether its Link is on is unknown, so a blocked network cannot be told apart.")
    if not channel.peer.link.enabled:
        return Verdict("link_off_remote", "The other laptop answers but has not joined Link (its LINK is off).")
    if not channel.peer.link.connected:
        return Verdict("link_off_remote", "The other laptop has joined Link but its Carabiner is not connected.")
    return None


def _judge_zero_peers(channel: PeerChannel, local: LocalLink, reach: ReachEvidence, hold_s: float) -> Verdict:
    """Both laptops joined, the partner answers, and this laptop counts 0 peers: for how long, and why."""
    assert channel.peer is not None
    remote = channel.peer.link
    if local.zero_peers_for_s is None or remote.connected_for_s is None:
        return Verdict("unknown", "How long Link has had no peer is not measured yet (no Carabiner status line on one side).")
    for_s = round(min(local.zero_peers_for_s, remote.connected_for_s), 1)
    if for_s < hold_s:
        return Verdict(
            "waiting",
            f"0 Link peers for {for_s} s with both laptops joined; Link usually finds a peer within a second, "
            f"so this waits {hold_s:g} s before naming the network.",
            for_s, round(hold_s - for_s, 1),
        )
    if reach.state == "heard" and reach.other_hosts_heard:
        return Verdict(
            "local_network_blocked",
            f"Link packets from {reach.other_hosts_heard} other host(s) arrive here, yet Carabiner has counted 0 peers for "
            f"{for_s} s: something on this laptop blocks Link (macOS: Privacy & Security > Local Network), not the network.",
            for_s,
        )
    heard = " No other host's Link packets were heard here." if reach.state != "unknown" else (
        f" The receive-only Link listener could not measure ({reach.reason}), so a macOS Local Network block is not ruled out."
    )
    return Verdict(
        "multicast_blocked",
        f"Carabiner has counted 0 Link peers for {for_s} s (hold {hold_s:g} s) while the other laptop answers over the "
        f"network ({channel.rtt_ms} ms) and reports its Link joined for {remote.connected_for_s:g} s.{heard}",
        for_s,
    )


# ---------------------------------------------------------------- Nearby


class NearbyHold:
    """B2BMCAST-02. Remembers since which finished scan the partner has been missing."""

    def __init__(self, hold_s: float = CFG.HOLD_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.hold_s = hold_s
        self._clock = clock
        self._absent_since: float | None = None

    def observe(self, channel: PeerChannel, nearby: NearbyEvidence) -> Verdict:
        verdict = self._judge(channel, nearby)
        if verdict.state not in ("waiting", "multicast_blocked"):
            self._absent_since = None
        return verdict

    def _judge(self, channel: PeerChannel, nearby: NearbyEvidence) -> Verdict:
        settled = _nearby_settled_before_scan(channel, nearby)
        if settled is not None:
            return settled
        if nearby.scan_error is not None:
            return Verdict("unknown", f"The last Bonjour scan failed: {nearby.scan_error}.")
        if nearby.scanned_at is not None and nearby.partner_listed is None and nearby.scan_note is not None:
            return Verdict("unknown", f"The last Bonjour scan was incomplete: {nearby.scan_note}.")
        if nearby.scanned_at is None or nearby.partner_listed is None:
            return Verdict("unknown", "No Bonjour scan has finished yet.")
        if nearby.partner_listed:
            return Verdict("ok", "Nearby lists the remembered partner.")
        if self._absent_since is None or nearby.scanned_at < self._absent_since:
            self._absent_since = nearby.scanned_at
        for_s = round(max(0.0, self._clock() - self._absent_since), 1)
        if for_s < self.hold_s:
            return Verdict(
                "waiting", f"The partner answers and lists itself, but Nearby has not seen it for {for_s} s.",
                for_s, round(self.hold_s - for_s, 1),
            )
        return Verdict(
            "multicast_blocked",
            f"The other laptop answers over the network and lists itself on Nearby, but no Bonjour scan here has "
            f"seen it for {for_s} s (hold {self.hold_s:g} s).",
            for_s,
        )


def _nearby_settled_before_scan(channel: PeerChannel, nearby: NearbyEvidence) -> Verdict | None:
    """A Nearby verdict that needs no scan: not paired, Nearby off, partner unreachable, silent or unlisted."""
    if not nearby.paired:
        return Verdict("not_paired", "Nearby recognizes only a remembered partner, and this laptop is not paired.")
    if not nearby.running:
        return Verdict("nearby_off_local", "Nearby is not listing this laptop, so it is not looking for the partner either.")
    if channel.state == "down":
        return Verdict("peer_unreachable", channel.reason)
    if channel.state == "none":
        return Verdict("unknown", "Paired, but no peer URL is set, so the partner cannot be asked.")
    if channel.report == "link_flag_off":
        return Verdict("nearby_off_remote", channel.reason)
    if channel.peer is None:
        return Verdict("unknown", f"{channel.reason} Whether it lists itself on Nearby is unknown.")
    if not channel.peer.nearby_listed:
        return Verdict("nearby_off_remote", "The other laptop answers but is not listing itself on Nearby.")
    return None


# ---------------------------------------------------------------- the answer


def _sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:]


def build_message(link: Verdict, nearby: Verdict, channel: PeerChannel) -> dict[str, Any] | None:
    """B2BMCAST-04: the panel copy, or None when there is nothing to tell the DJ."""
    label = (channel.peer.label if channel.peer else None) or UNNAMED_PARTNER
    link_blocked, nearby_blocked = link.state == "multicast_blocked", nearby.state == "multicast_blocked"
    if link_blocked or nearby_blocked:
        what = "Ableton Link and Nearby" if link_blocked and nearby_blocked else ("Ableton Link" if link_blocked else "Nearby")
        evidence = [v.reason for v, hit in ((link, link_blocked), (nearby, nearby_blocked)) if hit]
        return {
            "code": "multicast_blocked", "severity": "warn", "headline": BLOCKED_HEADLINE,
            "text": BLOCKED_TEXT.format(what=what, label=label), "fixes": list(FIXES), "title": " ".join(evidence),
        }
    if link.state == "link_off_remote":
        return {
            "code": "link_off_remote", "severity": "info", "headline": _sentence_case(LINK_OFF_HEADLINE.format(label=label)),
            "text": LINK_OFF_TEXT.format(label=label), "fixes": [], "title": link.reason,
        }
    return None


def diagnosis_answer(channel: PeerChannel, link: Verdict, nearby: Verdict, hold_s: float) -> dict[str, Any]:
    peer = channel.peer
    return {
        "hold_s": hold_s,
        "peer_channel": {k: v for k, v in asdict(channel).items() if k != "peer"},
        "peer": None if peer is None else {
            "label": peer.label, "link": asdict(peer.link), "nearby": {"listed": peer.nearby_listed},
        },
        "link": asdict(link),
        "nearby": asdict(nearby),
        "message": build_message(link, nearby, channel),
    }
