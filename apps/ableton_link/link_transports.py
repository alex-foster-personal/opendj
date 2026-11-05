"""LINK-32 / LINK-50..LINK-52: the possible network paths between two B2B laptops, and
two SEPARATE answers about them that used to be conflated under one "in use":

- PEER CHANNEL (``peer_channel``): the HTTP route to the other laptop's Open DJ
  (the B2B panel's PEER row URL). ``route -n get <ip>`` names the interface and
  ``networksetup -listallhardwareports`` names what it is. Its round trip is a
  rolling p50/p90 of TCP connects (one handshake = one RTT), never one sample.
- LINK (``link``): where Ableton Link peers are actually heard. Link finds peers
  by UDP multicast on every up interface; Carabiner exposes only a peer count,
  so the evidence is ``link_reach.LinkReachListener``: a receive-only listener
  on Link's discovery group that records which interface's subnet each Link
  node's packets come from. When that evidence cannot settle a row (listener
  down, still warming up, or Carabiner counts peers the listener has not
  heard) the row says ``unknown`` with the reason. Never a guess.

The two disagree in practice (Sat 10 Oct 2026, silver): peer channel on the
Thunderbolt cable while every Link peer was heard only on Wi-Fi; peer channel
over Tailscale (which carries no multicast) while Link heard nobody. A peer
channel off the local network while Link hears no peer is a ``warnings`` entry
with plain copy for the panel.

The cable can appear twice: macOS brings up the Thunderbolt Bridge AND an
anonymous "Ethernet Adapter (enN)" link (USB-C networking) over the same cable,
the latter with only a self-assigned 169.254 address. That sibling is the
cable, not a wired LAN (``classify_interface``).
"""

from __future__ import annotations

import errno
import ipaddress
import math
import re
import socket
import statistics
import subprocess
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import psutil

from apps.ableton_link.link_reach import HEARD_WINDOW_S, LinkReachSnapshot

TransportId = Literal["bridge", "wired", "wifi", "internet"]
PathKind = TransportId | Literal["vpn"]
Tri = Literal["yes", "no", "unknown"]

#: Fixed display order: fastest, most dependable path first.
TRANSPORT_ORDER: tuple[TransportId, ...] = ("bridge", "wired", "wifi", "internet")
TRANSPORT_LABELS: dict[TransportId, str] = {
    "bridge": "Cable (Thunderbolt Bridge)",
    "wired": "Wired LAN",
    "wifi": "Wi-Fi LAN",
    "internet": "Internet",
}
INTERNET_NOTE = (
    "Not available: Ableton Link finds peers by UDP multicast on the local network only. "
    "Over the internet it needs a VPN that carries multicast (ZeroTier does); "
    "Tailscale does not carry multicast, so Link cannot cross it."
)
LOCAL_NETWORK_FIX = "System Settings > Privacy & Security > Local Network"
WARN_PEER_CHANNEL_REMOTE = "Peer info over the internet; Link sync needs the same local network."
WARN_LOCAL_NETWORK_BLOCKED = f"macOS is blocking Open DJ from the local network: {LOCAL_NETWORK_FIX}"
WARN_LINK_ALONE = (
    "No Link peers found. Is the other laptop's Link on, on the same network? "
    f"If it is, macOS may be blocking Open DJ from the local network: {LOCAL_NETWORK_FIX}"
)
SUBPROCESS_TIMEOUT_S = 2.0
RTT_CONNECT_TIMEOUT_S = 0.5
#: Rolling window for the peer channel round trip; one sample on tonight's Wi-Fi is noise.
RTT_WINDOW = 20
#: "Ethernet Adapter (en4)": the name macOS gives an anonymous Ethernet-class link, which is
#: what a Mac-to-Mac USB-C/Thunderbolt cable brings up beside the Thunderbolt Bridge.
CABLE_SIBLING_PORT = re.compile(r"Ethernet Adapter \(en\d+\)")


@dataclass(frozen=True)
class NetInterface:
    device: str
    port: str
    ipv4: list[str]
    self_assigned: bool
    netmasks: list[str] = field(default_factory=list)


@dataclass
class RttStats:
    p50_ms: float | None
    p90_ms: float | None
    samples: int
    failures: int
    last_error: str | None


@dataclass
class LinkTransport:
    id: TransportId
    label: str
    available: bool
    #: Deprecated alias of ``peer_channel`` (LINK-32 shape), kept for older clients.
    in_use: Tri
    interfaces: list[NetInterface] = field(default_factory=list)
    #: Peer channel p50 TCP round trip, only on the row the peer channel uses.
    rtt_ms: float | None = None
    note: str = ""
    peer_channel: Tri = "unknown"
    link: Tri = "unknown"
    #: Distinct Link nodes heard on this path (None = could not listen).
    link_nodes: int | None = None
    link_note: str = ""
    rtt_p90_ms: float | None = None
    rtt_samples: int = 0


@dataclass
class PeerRoute:
    """How this laptop reaches the peer URL host. ``kind`` None = unknown."""

    peer_url: str | None
    host: str | None
    ip: str | None
    device: str | None
    kind: PathKind | None
    rtt_ms: float | None
    reason: str
    rtt: RttStats | None = None
    #: errno name of the last failed connect (EHOSTUNREACH on a LAN route = Local Network denied).
    connect_error: str | None = None


@dataclass
class LinkReachOut:
    state: Literal["heard", "none_heard", "unknown"]
    carabiner_connected: bool | None
    carabiner_peers: int | None
    heard_nodes: int | None
    own_nodes: int | None
    devices_heard: list[str]
    reason: str


@dataclass
class TransportWarning:
    code: Literal["peer_channel_remote", "local_network_blocked", "link_alone"]
    severity: Literal["warn", "info"]
    text: str
    title: str


# ---------------------------------------------------------------- interface census


def parse_hardware_ports(text: str) -> dict[str, str]:
    """``networksetup -listallhardwareports`` -> {device: hardware port name}."""
    ports: dict[str, str] = {}
    for block in text.split("\n\n"):
        port = re.search(r"^Hardware Port: (.+)$", block, re.MULTILINE)
        device = re.search(r"^Device: (\S+)$", block, re.MULTILINE)
        if port and device:
            ports[device.group(1)] = port.group(1).strip()
    return ports


#: Linux ``/sys/class/net/<dev>/type`` for Ethernet-framed links (ARPHRD_ETHER).
_ARPHRD_ETHER = "1"


def linux_hardware_ports(sys_class_net: Path = Path("/sys/class/net")) -> dict[str, str]:
    """B2BPAIR-09: Linux's answer to ``networksetup -listallhardwareports``, from sysfs.

    A device with a ``wireless`` dir is "Wi-Fi"; any other Ethernet-framed device (a NIC, a
    bridge, a veth or dummy link) is "Ethernet", which ``classify_interface`` reads as wired.
    Loopback and tunnels are left out, so they stay unclassified (not a Link LAN path).
    """
    ports: dict[str, str] = {}
    for device in sorted(sys_class_net.iterdir()):
        if (device / "wireless").exists():
            ports[device.name] = "Wi-Fi"
        elif (device / "type").read_text().strip() == _ARPHRD_ETHER:
            ports[device.name] = "Ethernet"
    return ports


def _all_link_local(ipv4: Sequence[str]) -> bool:
    return bool(ipv4) and all(ipaddress.ip_address(ip).is_link_local for ip in ipv4)


def classify_interface(device: str, port: str | None, ipv4: Sequence[str] = ()) -> PathKind | None:
    """What a device is as a Link path. None = not a Link LAN path (loopback, AWDL, ...)."""
    if device.startswith(("utun", "ipsec", "ppp")):
        return "vpn"
    if port is None:
        return None
    if port == "Thunderbolt Bridge" or device.startswith("bridge"):
        return "bridge"
    if port in ("Wi-Fi", "AirPort"):
        return "wifi"
    # "Thunderbolt 1..N" are the bridge's member ports, not paths of their own.
    if re.fullmatch(r"Thunderbolt \d+", port):
        return None
    # The cable's second link: anonymous "Ethernet Adapter (enN)" with only a self-assigned
    # address (no DHCP server on a Mac-to-Mac cable). A named dongle or a DHCP lease is wired.
    if CABLE_SIBLING_PORT.fullmatch(port) and _all_link_local(ipv4):
        return "bridge"
    if "Ethernet" in port or "LAN" in port:
        return "wired"
    return None


def _run(argv: list[str]) -> str:
    """Run a macOS network tool; any failure raises with the tool's own stderr."""
    done = subprocess.run(
        argv, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL
    )
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} exited {done.returncode}: {done.stderr.strip()[:300]}")
    return done.stdout


def up_interfaces(hardware_ports: dict[str, str]) -> list[tuple[NetInterface, PathKind]]:
    """Up interfaces carrying IPv4, each with its Link path kind."""
    stats = psutil.net_if_stats()
    found: list[tuple[NetInterface, PathKind]] = []
    for device, addrs in sorted(psutil.net_if_addrs().items()):
        stat = stats.get(device)
        v4 = [a for a in addrs if a.family == socket.AF_INET]
        ipv4 = [a.address for a in v4]
        if stat is None or not stat.isup or not ipv4:
            continue
        kind = classify_interface(device, hardware_ports.get(device), ipv4)
        if kind is None:
            continue
        iface = NetInterface(
            device, hardware_ports.get(device, device), ipv4, _all_link_local(ipv4), [a.netmask or "255.255.255.255" for a in v4]
        )
        found.append((iface, kind))
    return found


def local_ipv4_addresses() -> list[str]:
    """Every IPv4 address on this host, Link LAN path or not (loopback, wg0, tailscale0)."""
    return sorted({a.address for addrs in psutil.net_if_addrs().values() for a in addrs if a.family == socket.AF_INET})


def interface_subnets(interfaces: list[tuple[NetInterface, PathKind]]) -> dict[str, list[tuple[str, str]]]:
    """{device: [(ip, netmask)]} for ``LinkReachListener.snapshot``."""
    return {i.device: list(zip(i.ipv4, i.netmasks, strict=True)) for i, _ in interfaces}


# ---------------------------------------------------------------- peer channel round trip


class RttWindow:
    """Last ``RTT_WINDOW`` TCP-connect round trips per peer URL, p50 + p90 + count."""

    def __init__(self, size: int = RTT_WINDOW) -> None:
        self.size = size
        self._samples: dict[str, deque[float]] = {}
        self._failures: dict[str, int] = {}
        self._last_error: dict[str, str | None] = {}

    def add(self, key: str, rtt_ms: float | None, error: str | None) -> None:
        window = self._samples.setdefault(key, deque(maxlen=self.size))
        if rtt_ms is None:
            self._failures[key] = self._failures.get(key, 0) + 1
            self._last_error[key] = error
            return
        window.append(rtt_ms)
        self._last_error[key] = None

    def stats(self, key: str) -> RttStats:
        samples = sorted(self._samples.get(key, ()))
        if not samples:
            return RttStats(None, None, 0, self._failures.get(key, 0), self._last_error.get(key))
        p90 = samples[max(0, math.ceil(0.9 * len(samples)) - 1)]
        return RttStats(
            round(statistics.median(samples), 2), round(p90, 2), len(samples),
            self._failures.get(key, 0), self._last_error.get(key),
        )


def _tcp_rtt_ms(ip: str, port: int) -> tuple[float | None, str | None]:
    """One TCP connect: (round trip ms, None) or (None, errno name / timeout)."""
    started = time.perf_counter()
    try:
        with socket.create_connection((ip, port), timeout=RTT_CONNECT_TIMEOUT_S):
            pass
    except TimeoutError:
        return None, "timeout"
    except OSError as exc:
        return None, errno.errorcode.get(exc.errno or 0, type(exc).__name__)
    return round((time.perf_counter() - started) * 1000.0, 2), None


def parse_route_interface(text: str) -> str | None:
    match = re.search(r"^\s*interface: (\S+)$", text, re.MULTILINE)
    return match.group(1) if match else None


def route_has_gateway(text: str) -> bool:
    """``route -n get`` names a gateway when the host is NOT on the interface's own link."""
    return re.search(r"^\s*gateway: \S+$", text, re.MULTILINE) is not None


def peer_route(peer_url: str | None, hardware_ports: dict[str, str], rtt_window: RttWindow) -> PeerRoute:
    if peer_url is None:
        return PeerRoute(None, None, None, None, None, None, "No peer URL is set (PEER row), so the path to the other laptop is unknown.")
    parsed = urlparse(peer_url)
    host = parsed.hostname
    if host is None:
        return PeerRoute(peer_url, None, None, None, None, None, f"Peer URL {peer_url} has no host, so the path is unknown.")
    try:
        ip = str(socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0])
    except OSError as exc:
        return PeerRoute(peer_url, host, None, None, None, None, f"Peer host {host} does not resolve ({exc}), so the path is unknown.")
    if ipaddress.ip_address(ip).is_loopback:
        return PeerRoute(
            peer_url, host, ip, None, None, None,
            f"Peer URL {peer_url} is a loopback tunnel (SSH); the tunnel's real network path is not visible here.",
        )
    try:
        route_text = _run(["route", "-n", "get", ip])
    except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
        # TimeoutExpired, a missing `route` binary, or a non-zero exit: say so, never guess.
        return PeerRoute(
            peer_url, host, ip, None, None, None,
            f"route -n get {ip} failed ({type(exc).__name__}: {exc}), so the path is unknown.",
        )
    device = parse_route_interface(route_text)
    if device is None:
        return PeerRoute(peer_url, host, ip, None, None, None, f"route -n get {ip} named no interface, so the path is unknown.")
    v4 = [a.address for a in psutil.net_if_addrs().get(device, []) if a.family == socket.AF_INET]
    kind = classify_interface(device, hardware_ports.get(device), v4)
    routed = route_has_gateway(route_text) and kind in ("bridge", "wired", "wifi")
    if routed:
        kind = "internet"  # Beyond this LAN through a gateway: multicast (Link) does not follow.
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    rtt, error = _tcp_rtt_ms(ip, port)
    rtt_window.add(peer_url, rtt, error)
    stats = rtt_window.stats(peer_url)
    rtt_text = (
        f"TCP connect round trip p50 {stats.p50_ms} ms, p90 {stats.p90_ms} ms over the last {stats.samples} samples"
        if stats.samples else f"no TCP answer from {ip}:{port} yet ({error})"
    )
    where = "through a gateway, beyond the local network" if routed else hardware_ports.get(device, "no hardware port")
    return PeerRoute(
        peer_url, host, ip, device, kind, stats.p50_ms,
        f"The route to the peer {host} ({ip}) leaves on {device} ({where}); {rtt_text}.",
        stats, error,
    )


# ---------------------------------------------------------------- Link's own reach


@dataclass(frozen=True)
class CarabinerView:
    """What GET /api/v1/link says: None fields = Link is off or Carabiner silent."""

    connected: bool | None
    peers: int | None


def link_reach(reach: LinkReachSnapshot, carabiner: CarabinerView) -> LinkReachOut:
    heard = sum(len(v) for v in reach.nodes_by_device.values()) + len(reach.unmatched_nodes) if reach.listening else None
    devices = sorted(reach.nodes_by_device)
    base = dict(
        carabiner_connected=carabiner.connected, carabiner_peers=carabiner.peers, heard_nodes=heard,
        own_nodes=len(reach.own_nodes) if reach.listening else None, devices_heard=devices,
    )
    if not reach.listening:
        return LinkReachOut(state="unknown", reason=f"Link's path cannot be measured: {reach.reason}.", **base)
    if reach.listening_for_s < HEARD_WINDOW_S and not heard:
        return LinkReachOut(
            state="unknown", reason=f"Listening for Link peers for only {reach.listening_for_s} s; ask again shortly.", **base
        )
    if heard:
        where = ", ".join(f"{d} ({len(reach.nodes_by_device[d])})" for d in devices) or "an unmatched subnet"
        return LinkReachOut(state="heard", reason=f"Link peers heard by multicast on: {where}.", **base)
    return LinkReachOut(state="none_heard", reason="No other Link peer's multicast was heard on any interface.", **base)


def _row_link(devices: list[str], reach: LinkReachSnapshot, out: LinkReachOut) -> tuple[Tri, int | None, str]:
    if out.state == "unknown":
        return "unknown", None, out.reason
    nodes = sum(len(reach.nodes_by_device.get(d, [])) for d in devices)
    if nodes:
        return "yes", nodes, f"Link peers heard on this path: {nodes}."
    unaccounted = (out.carabiner_peers or 0) - (out.heard_nodes or 0)
    if unaccounted > 0:
        return "unknown", 0, (
            f"No Link peer heard on this path, but Carabiner counts {out.carabiner_peers} peers and only "
            f"{out.heard_nodes} were heard; Carabiner does not say which path the rest use."
        )
    return "no", 0, "No Link peer heard on this path."


def transport_warnings(route: PeerRoute, out: LinkReachOut) -> list[TransportWarning]:
    warnings: list[TransportWarning] = []
    link_peers = out.carabiner_peers if out.carabiner_peers is not None else out.heard_nodes
    if route.kind in ("vpn", "internet"):
        quiet = not link_peers
        warnings.append(TransportWarning(
            "peer_channel_remote", "warn" if quiet else "info",
            WARN_PEER_CHANNEL_REMOTE if quiet else "Peer info goes over the internet; Link peers are on the local network.",
            f"The PEER row's URL is reached over {route.device} ({route.kind}). That carries track info and shouts, "
            "but Ableton Link only syncs over UDP multicast on the local network, which a VPN like Tailscale does not carry. "
            + out.reason,
        ))
    lan_route = route.kind in ("bridge", "wired", "wifi")
    blocked_by_connect = lan_route and route.connect_error == "EHOSTUNREACH"
    blocked_by_reach = out.carabiner_connected is True and out.carabiner_peers == 0 and bool(out.heard_nodes)
    if blocked_by_connect or blocked_by_reach:
        evidence = []
        if blocked_by_reach:
            evidence.append(
                f"Link peers are on the network ({out.heard_nodes} heard on {', '.join(out.devices_heard) or 'an unmatched subnet'}) "
                "but this laptop's Link counts 0 peers: it hears them and cannot answer."
            )
        if blocked_by_connect:
            evidence.append(f"Connecting to the peer on the local network failed with EHOSTUNREACH ({route.ip}).")
        warnings.append(TransportWarning(
            "local_network_blocked", "warn", WARN_LOCAL_NETWORK_BLOCKED,
            " ".join(evidence) + " macOS Local Network privacy blocks an app's local traffic this way (Apple TN3179). "
            f"Turn Open DJ on in {LOCAL_NETWORK_FIX}, then leave and rejoin Link.",
        ))
    elif out.carabiner_connected is True and out.carabiner_peers == 0 and out.state != "unknown":
        warnings.append(TransportWarning(
            "link_alone", "warn", WARN_LINK_ALONE,
            "Carabiner counts 0 Link peers and no other Link peer was heard on any interface. Likely causes: the other "
            "laptop's Link is off, it is on another network, or macOS Local Network privacy blocks Open DJ (the block is "
            "not always visible from here).",
        ))
    return warnings


# ---------------------------------------------------------------- the answer


def build_transports(
    hardware_ports: dict[str, str],
    peer_url: str | None,
    *,
    rtt_window: RttWindow,
    reach: LinkReachSnapshot,
    carabiner: CarabinerView,
    interfaces: list[tuple[NetInterface, PathKind]] | None = None,
) -> dict[str, object]:
    interfaces = up_interfaces(hardware_ports) if interfaces is None else interfaces
    route = peer_route(peer_url, hardware_ports, rtt_window)
    reach_out = link_reach(reach, carabiner)
    stats = route.rtt
    transports: list[LinkTransport] = []
    for tid in TRANSPORT_ORDER:
        mine = [iface for iface, kind in interfaces if kind == tid]
        channel: Tri = "unknown" if route.kind is None else ("yes" if route.kind == tid or (tid == "internet" and route.kind == "vpn") else "no")
        rtt = (stats.p50_ms, stats.p90_ms, stats.samples) if channel == "yes" and stats else (None, None, 0)
        if tid == "internet":
            vpn = [iface for iface, kind in interfaces if kind == "vpn"]
            note = INTERNET_NOTE
            if channel == "yes":
                note += f" The peer URL is reached over {route.device}: peer info works, Link will not find the peer this way."
            link_state, link_nodes, link_note = _row_link([i.device for i in vpn], reach, reach_out)
            if link_state == "unknown" and reach_out.state != "unknown":
                link_state, link_note = "no", "Link multicast does not cross the internet or Tailscale."
            transports.append(LinkTransport(
                tid, TRANSPORT_LABELS[tid], False, channel, [], rtt[0], note, channel,
                link_state, link_nodes, link_note, rtt[1], rtt[2],
            ))
            continue
        if mine:
            names = ", ".join(f"{i.device} {', '.join(i.ipv4)}{' (self-assigned)' if i.self_assigned else ''}" for i in mine)
            note = f"Up on this laptop: {names}."
        else:
            note = "No up interface of this kind on this laptop."
        if channel == "yes":
            note += " Peer channel: " + route.reason
        link_state, link_nodes, link_note = _row_link([i.device for i in mine], reach, reach_out)
        transports.append(LinkTransport(
            tid, TRANSPORT_LABELS[tid], bool(mine), channel, mine, rtt[0], note, channel,
            link_state, link_nodes, link_note, rtt[1], rtt[2],
        ))
    return {
        "transports": [asdict(t) for t in transports],
        "peer": asdict(route),
        "link_reach": asdict(reach_out),
        "warnings": [asdict(w) for w in transport_warnings(route, reach_out)],
        "basis": (
            "peer_channel = the route to the PEER row's host (HTTP between the laptops); "
            "link = where Ableton Link peers' multicast is heard (receive-only listener); in_use = peer_channel (deprecated)"
        ),
    }


def parse_ifconfig_ipv4(text: str) -> dict[str, list[tuple[str, str]]]:
    """``ifconfig`` -> {device: [(ipv4, dotted netmask)]}; for fixtures and diagnostics."""
    found: dict[str, list[tuple[str, str]]] = {}
    device = ""
    for line in text.splitlines():
        header = re.match(r"^(\S+?):\s", line)
        if header:
            device = header.group(1)
            continue
        inet = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+) netmask 0x([0-9a-f]{8})", line)
        if inet and device:
            found.setdefault(device, []).append((inet.group(1), str(ipaddress.IPv4Address(int(inet.group(2), 16)))))
    return found
