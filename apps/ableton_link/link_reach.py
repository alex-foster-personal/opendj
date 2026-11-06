"""LINK-50: where Ableton Link peers are actually heard, by passive multicast listening.

Carabiner reports a peer COUNT, never which interface a peer is on, so the
B2B panel cannot read Link's own path from it. This module listens (it never
sends) on Link's discovery group, 224.76.78.75:20808, joined on every up IPv4
interface, and records each Link node it hears: the node id from the message
header and the source address. Matching the source address to the interface
subnet that contains it says which path that node is reachable on.

What it does and does not prove:
- A node heard on bridge0 is a Link peer reachable over the cable; a node
  heard only on en0 is reachable only over Wi-Fi. That is evidence, not a guess.
- It is a LOWER bound. Carabiner can count peers this listener has not heard
  (other Link apps on this same laptop, a node between broadcasts), so the
  route compares the two and says UNKNOWN when they disagree.
- Joining a multicast group is a receive-side socket option: it does not join
  the Link session and sends no packet.

Link's wire header (ableton/link discovery v1): ``_asdp_v`` + version byte
(8 bytes), then message type (1 = alive, 2 = response, 3 = bye-bye), ttl,
a 2-byte group id and the 8-byte node id.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

LINK_MULTICAST_GROUP = "224.76.78.75"
LINK_MULTICAST_PORT = 20808
LINK_HEADER = b"_asdp_v\x01"
MSG_ALIVE, MSG_RESPONSE, MSG_BYEBYE = 1, 2, 3
#: A node is "heard" for this long after its last message (Link's own alive ttl is 5 s).
HEARD_WINDOW_S = 6.0
RECV_TIMEOUT_S = 0.5


@dataclass(frozen=True)
class LinkMessage:
    kind: int
    ttl: int
    node_id: str


def parse_link_message(data: bytes) -> LinkMessage | None:
    """A Link discovery datagram -> its header; None for anything else on the port."""
    if len(data) < 20 or not data.startswith(LINK_HEADER):
        return None
    return LinkMessage(kind=data[8], ttl=data[9], node_id=data[12:20].hex())


def interface_for_source(source_ip: str, interfaces: dict[str, list[tuple[str, str]]]) -> str | None:
    """Which local interface's subnet contains ``source_ip``. ``interfaces`` = {device: [(ip, netmask)]}."""
    source = ipaddress.ip_address(source_ip)
    for device, addrs in sorted(interfaces.items()):
        for ip, netmask in addrs:
            if source in ipaddress.ip_network(f"{ip}/{netmask}", strict=False):
                return device
    return None


@dataclass
class _Sighting:
    source_ip: str
    last_seen: float
    gone: bool = False


@dataclass
class LinkReachSnapshot:
    """``listening`` False means the listener could not run: ``reason`` says why (UNKNOWN, never zero)."""

    listening: bool
    reason: str
    joined: list[str] = field(default_factory=list)
    #: device -> distinct Link node ids heard on that device's subnet (other hosts only).
    nodes_by_device: dict[str, list[str]] = field(default_factory=dict)
    #: Link nodes heard from this laptop's own addresses (its own Link apps, multicast loopback).
    own_nodes: list[str] = field(default_factory=list)
    #: Heard from an address no local subnet contains.
    unmatched_nodes: list[str] = field(default_factory=list)
    listening_for_s: float = 0.0


class LinkReachListener:
    """One daemon thread per process; ``snapshot`` is cheap and thread-safe."""

    def __init__(self, group: str = LINK_MULTICAST_GROUP, port: int = LINK_MULTICAST_PORT) -> None:
        self.group = group
        self.port = port
        self._lock = threading.Lock()
        self._sightings: dict[str, _Sighting] = {}
        self._joined: set[str] = set()
        self._sock: socket.socket | None = None
        self._error: str | None = None
        self._started_at: float | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            reuseport = getattr(socket, "SO_REUSEPORT", None)
            if reuseport is not None:
                sock.setsockopt(socket.SOL_SOCKET, reuseport, 1)
            # A multicast group listener: Linux delivers group datagrams only to a socket bound to
            # the wildcard (or the group) address, and membership is joined per interface below.
            # nosemgrep: python.lang.security.audit.network.bind.avoid-bind-to-all-interfaces
            sock.bind(("", self.port))
            sock.settimeout(RECV_TIMEOUT_S)
        except OSError as exc:
            self._error = f"could not listen on UDP {self.port} ({exc})"
            return
        self._sock = sock
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._recv_loop, name="link-reach-listener", daemon=True)
        self._thread.start()

    def join(self, local_ips: list[str]) -> None:
        """Join the group on each local IPv4 address not yet joined (interfaces come and go)."""
        if self._sock is None:
            return
        for ip in local_ips:
            if ip in self._joined:
                continue
            try:
                self._sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(self.group) + socket.inet_aton(ip)
                )
            except OSError:
                continue  # Already a member through another address on the same link; nothing to record.
            self._joined.add(ip)

    def _recv_loop(self) -> None:
        assert self._sock is not None
        while True:
            try:
                data, (source_ip, _port) = self._sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError as exc:
                with self._lock:
                    self._error = f"listener stopped: {exc}"
                return
            message = parse_link_message(data)
            if message is None:
                continue
            with self._lock:
                self._sightings[message.node_id] = _Sighting(source_ip, time.monotonic(), gone=message.kind == MSG_BYEBYE)

    def snapshot(
        self, interfaces: dict[str, list[tuple[str, str]]], now: float | None = None, own_ips: Iterable[str] = ()
    ) -> LinkReachSnapshot:
        """``own_ips``: every address of this host. The census in ``interfaces`` lists Link LAN paths
        only, so a node looped back on a tunnel (wg0, tailscale0) would otherwise read as another host."""
        if self._sock is None or self._error is not None:
            reason = self._error or "the listener has not started"
            return LinkReachSnapshot(listening=False, reason=reason)
        now = time.monotonic() if now is None else now
        own_ips = {ip for addrs in interfaces.values() for ip, _ in addrs} | set(own_ips)
        by_device: dict[str, list[str]] = {}
        own: list[str] = []
        unmatched: list[str] = []
        with self._lock:
            live = {nid: s for nid, s in self._sightings.items() if not s.gone and now - s.last_seen <= HEARD_WINDOW_S}
        for node_id, sighting in sorted(live.items()):
            if sighting.source_ip in own_ips:
                own.append(node_id)
                continue
            device = interface_for_source(sighting.source_ip, interfaces)
            if device is None:
                unmatched.append(node_id)
            else:
                by_device.setdefault(device, []).append(node_id)
        assert self._started_at is not None
        return LinkReachSnapshot(
            listening=True,
            reason=f"listening on {self.group}:{self.port} (receive only)",
            joined=sorted(self._joined),
            nodes_by_device=by_device,
            own_nodes=own,
            unmatched_nodes=unmatched,
            listening_for_s=round(now - self._started_at, 1),
        )
