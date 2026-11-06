"""B2BPAIR-02/09: DNS-SD over mDNS for B2B invites, through the system's own responder.

The inviter advertises ``_opendj-b2b._tcp`` while an invite is open; the joiner browses,
resolves each instance to host + port + TXT, then reads the host's IPv4 addresses with the
interface each answer arrived on, which names the path (cable bridge, wired, Wi-Fi).

It drives ``/usr/bin/dns-sd`` (Apple's DNS-SD client of the system responder) rather than a
second in-process responder competing for UDP 5353: on macOS the system responder IS the
Bonjour implementation, and python-zeroconf is LGPL, which the license gate refuses.
On Linux (B2BPAIR-09) the system responder is Avahi, driven the same way through its own
clients ``avahi-browse`` and ``avahi-publish``: same ``Resolved`` records, same ``Advertiser``
shape. The backend is chosen by which client exists (``dns-sd`` first); with neither,
``available()`` is False and the routes answer ``mdns_unavailable`` instead of pretending to
search. An Avahi client that cannot reach its daemon raises ``MdnsUnavailable`` with Avahi's
own words, never an empty result.

B2BNEAR-06: a scan always ends inside ``SCAN_BUDGET_S``. Each client's listening window
starts at its first byte (a loaded machine can take seconds just to start ``dns-sd``), and a
client that prints nothing within ``STARTUP_S`` is a ``ScanTimeout``, never an empty list.
Instances resolve in parallel; one that does not answer in time is reported in
``ScanResult.unresolved`` beside the ones that did, so "nobody there" and "the machine was too
slow to hear everyone" read differently.

``MDT_B2B_MDNS_TYPE`` overrides the service type (tests use a random one so two runs, or a
real B2B pair on the same LAN, never see each other's invites).
"""

from __future__ import annotations

import os
import re
import select
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

DNS_SD = "/usr/bin/dns-sd"
AVAHI_BROWSE = "avahi-browse"
AVAHI_PUBLISH = "avahi-publish"
SERVICE_TYPE_ENV = "MDT_B2B_MDNS_TYPE"
DEFAULT_SERVICE_TYPE = "_opendj-b2b._tcp"
AF_SERVICE_ID = "com.af.music-dj-tools.b2b-mdns"
BROWSE_S = 1.5
#: Ends early once the answer is in, so a longer window only costs time for a silent instance.
RESOLVE_S = 3.0
ADDRESS_S = 0.8
#: How long a client may take to print its first byte. Each window above starts from that byte.
STARTUP_S = 4.0
#: A whole scan (browse, then every resolve in parallel) never runs longer than this.
SCAN_BUDGET_S = 10.0
#: A browse client slower than this to start means a loaded machine: the list may be incomplete.
SLOW_START_S = 0.5
MAX_PARALLEL_RESOLVES = 8
#: After SIGTERM, how long a client gets to exit before SIGKILL.
TERM_GRACE_S = 1.0
#: avahi-browse -t exits on its own once the cache is dumped; this only bounds a wedged daemon.
AVAHI_BROWSE_TIMEOUT_S = 5.0
#: avahi-publish says "Established under name" on stderr once the record is live.
AVAHI_ESTABLISH_S = 3.0

_BROWSE_LINE = re.compile(r"^\s*\S+\s+Add\s+\d+\s+(\d+)\s+(\S+)\s+(\S+)\s+(.+?)\s*$")
_RESOLVE_LINE = re.compile(r"can be reached at (\S+?)\.?:(\d+) \(interface (\d+)\)")
_ADDRESS_LINE = re.compile(r"^\s*\S+\s+Add\s+\S+\s+(\d+)\s+\S+\s+(\d+\.\d+\.\d+\.\d+)\s+\d+\s*$")


def service_type() -> str:
    return os.environ.get(SERVICE_TYPE_ENV) or DEFAULT_SERVICE_TYPE


class MdnsUnavailable(RuntimeError):
    """The system mDNS responder could not be used; the message carries its own reason."""


class ScanTimeout(MdnsUnavailable):
    """The responder client did not answer inside the scan's time budget."""


def backend() -> str | None:
    """``"dns-sd"`` (macOS), ``"avahi"`` (Linux), or None when neither client is installed."""
    if Path(DNS_SD).exists():
        return "dns-sd"
    if shutil.which(AVAHI_BROWSE) and shutil.which(AVAHI_PUBLISH):
        return "avahi"
    return None


def available() -> bool:
    return backend() is not None


def client_name() -> str:
    """The responder client this machine would use, for error messages."""
    return {"dns-sd": DNS_SD, "avahi": f"{AVAHI_BROWSE}/{AVAHI_PUBLISH}"}.get(backend() or "", f"{DNS_SD} or {AVAHI_BROWSE}")


def _env() -> dict[str, str]:
    return {**os.environ, "AF_SERVICE_ID": AF_SERVICE_ID}


@dataclass(frozen=True)
class Resolved:
    instance: str
    host: str
    port: int
    txt: dict[str, str]
    #: (ipv4, local interface name the answer arrived on)
    addresses: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class ScanResult:
    resolved: list[Resolved]
    #: Instances the browse saw that did not resolve inside the budget.
    unresolved: list[str] = field(default_factory=list)
    #: Why the list may be incomplete (slow start, unresolved instances); empty when complete.
    notes: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.unresolved or self.notes)


@dataclass(frozen=True)
class _Collected:
    lines: list[str]
    #: Seconds from spawn to the first byte; None = the client printed nothing in time.
    started_after_s: float | None


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM, then SIGKILL if it is still running after ``TERM_GRACE_S``; always reaped."""
    proc.terminate()
    try:
        proc.wait(timeout=TERM_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _window_end(spawned: float, first_byte: float | None, window_s: float, deadline: float) -> float:
    """``STARTUP_S`` from spawn until the first byte, then ``window_s`` from it; never past ``deadline``."""
    return min(deadline, spawned + STARTUP_S if first_byte is None else first_byte + window_s)


def _read_pty(master: int) -> bytes:
    """One read; empty once the child has closed its side (EOF or EIO)."""
    try:
        return os.read(master, 65536)
    except OSError:  # EIO: the child closed its side
        return b""


def _collect(argv: list[str], window_s: float, deadline: float, enough: int | None = None) -> _Collected:
    """Run a streaming dns-sd query and return its lines.

    Listens for ``window_s`` from the client's first byte, waits at most ``STARTUP_S`` for that
    byte, and never past ``deadline`` (``time.monotonic``). dns-sd block-buffers a pipe and only
    line-buffers a terminal, so it writes to a pty.
    """
    try:
        import pty
    except ImportError as exc:
        raise MdnsUnavailable("dns-sd needs a PTY, which this platform does not provide") from exc
    master, slave = pty.openpty()
    spawned = time.monotonic()
    proc = subprocess.Popen(argv, stdout=slave, stderr=slave, stdin=subprocess.DEVNULL, env=_env(), close_fds=True)
    os.close(slave)
    buffer = b""
    first_byte: float | None = None
    try:
        while (left := _window_end(spawned, first_byte, window_s, deadline) - time.monotonic()) > 0:
            if not select.select([master], [], [], left)[0]:
                continue
            chunk = _read_pty(master)
            if not chunk:
                break
            first_byte = first_byte or time.monotonic()
            buffer += chunk
            if enough is not None and buffer.count(b"\n") >= enough + 2:
                break
    finally:
        _stop(proc)
        os.close(master)
    lines = [line.rstrip("\r") for line in buffer.decode(errors="replace").split("\n") if line.strip()]
    return _Collected(lines, None if first_byte is None else first_byte - spawned)


def _interface_name(index: int) -> str:
    try:
        return socket.if_indextoname(index)
    except OSError:
        return f"if{index}"


def parse_browse(lines: list[str], stype: str) -> list[str]:
    names: list[str] = []
    for line in lines:
        match = _BROWSE_LINE.match(line)
        if match and match.group(3).rstrip(".") == stype and match.group(4) not in names:
            names.append(match.group(4))
    return names


def parse_resolve(lines: list[str]) -> tuple[str, int, dict[str, str]] | None:
    for i, line in enumerate(lines):
        match = _RESOLVE_LINE.search(line)
        if match and i + 1 < len(lines):
            txt = dict(part.split("=", 1) for part in lines[i + 1].split() if "=" in part)
            return match.group(1), int(match.group(2)), txt
    return None


def parse_addresses(lines: list[str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for line in lines:
        match = _ADDRESS_LINE.match(line)
        if match and match.group(2) != "0.0.0.0":
            pair = (match.group(2), _interface_name(int(match.group(1))))
            if pair not in found:
                found.append(pair)
    return found


def scan(stype: str | None = None, budget_s: float | None = None) -> ScanResult:
    """Every instance of the service on the LAN right now, resolved with its addresses, inside
    ``budget_s`` (default ``SCAN_BUDGET_S``). Raises ``ScanTimeout`` when the browse itself never
    answered, ``MdnsUnavailable`` when the responder cannot be used."""
    stype = stype or service_type()
    budget = SCAN_BUDGET_S if budget_s is None else budget_s
    deadline = time.monotonic() + budget
    if backend() == "avahi":
        return ScanResult(_avahi_browse_and_resolve(stype, deadline))
    return _dns_sd_scan(stype, deadline, budget)


def _dns_sd_scan(stype: str, deadline: float, budget: float) -> ScanResult:
    browse = _collect([DNS_SD, "-B", stype, "local"], BROWSE_S, deadline)
    if browse.started_after_s is None:
        raise ScanTimeout(f"{DNS_SD} -B printed nothing within {min(STARTUP_S, budget):g} s (machine overloaded?)")
    notes: list[str] = []
    if browse.started_after_s > SLOW_START_S:
        notes.append(f"{DNS_SD} took {browse.started_after_s:.1f} s to start, so this machine is loaded "
                     "and the list may be incomplete")
    names = parse_browse(browse.lines, stype)
    if not names:
        return ScanResult([], [], notes)
    with ThreadPoolExecutor(max_workers=min(len(names), MAX_PARALLEL_RESOLVES), thread_name_prefix="b2b-mdns") as pool:
        outcomes = list(pool.map(lambda name: _dns_sd_resolve(name, stype, deadline), names))
    unresolved = [name for name, outcome in zip(names, outcomes, strict=True) if outcome is None]
    if unresolved:
        notes.append(f"{len(unresolved)} of {len(names)} Open DJs found did not answer in time")
    return ScanResult([outcome for outcome in outcomes if outcome is not None], unresolved, notes)


def _dns_sd_resolve(name: str, stype: str, deadline: float) -> Resolved | None:
    resolved = parse_resolve(_collect([DNS_SD, "-L", name, stype, "local"], RESOLVE_S, deadline, enough=3).lines)
    if resolved is None:
        return None
    host, port, txt = resolved
    addresses = parse_addresses(_collect([DNS_SD, "-G", "v4", f"{host}."], ADDRESS_S, deadline).lines)
    return Resolved(name, host, port, txt, addresses)


class Advertiser:
    """A live ``dns-sd -R`` / ``avahi-publish -s`` registration; ``stop()`` withdraws it (goodbye packet)."""

    def __init__(self, instance: str, port: int, txt: dict[str, str], stype: str | None = None) -> None:
        self.instance = instance
        self.port = port
        pairs = [f"{k}={v}" for k, v in txt.items()]
        if backend() == "avahi":
            self._proc = _avahi_publish([AVAHI_PUBLISH, "-s", instance, stype or service_type(), str(port), *pairs])
            return
        argv = [DNS_SD, "-R", instance, stype or service_type(), "local", str(port), *pairs]
        self._proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env())

    def alive(self) -> bool:
        return self._proc.poll() is None

    def stop(self) -> None:
        if self._proc.poll() is None:
            _stop(self._proc)


# ----------------------------------------------------------------------------- Avahi (Linux)

_AVAHI_ESCAPE = re.compile(r"\\(\d{3}|.)")
_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def _avahi_unescape(text: str) -> str:
    """avahi-browse -p escapes names: backslash + 3 decimal digits is one byte (032 = space),
    backslash + any other character is that character."""
    return _AVAHI_ESCAPE.sub(lambda m: chr(int(m.group(1))) if m.group(1).isdigit() else m.group(1), text)


def parse_avahi_resolved(lines: list[str], stype: str) -> list[Resolved]:
    """``avahi-browse -p -r`` ``=`` lines -> one ``Resolved`` per instance, IPv4 addresses only.

    Fields: ``=;iface;proto;name;type;domain;host;address;port;"k=v" ...``. Avahi answers once
    per interface, so the same instance on two interfaces is one record with two addresses.
    """
    found: dict[str, Resolved] = {}
    for line in lines:
        parts = line.split(";", 9)
        if len(parts) < 9 or parts[0] != "=" or parts[2] != "IPv4" or parts[4] != stype:
            continue
        name = _avahi_unescape(parts[3])
        txt_field = parts[9] if len(parts) > 9 else ""
        txt = dict(item.split("=", 1) for item in re.findall(r'"([^"]*)"', txt_field) if "=" in item)
        record = found.setdefault(name, Resolved(name, parts[6], int(parts[8]), txt))
        address = (parts[7], parts[1])
        # Avahi can answer an IPv4 browse with the host's IPv6 address; the door is IPv4 only.
        if _IPV4.fullmatch(parts[7]) and address not in record.addresses:
            record.addresses.append(address)
    return list(found.values())


def _avahi_browse_and_resolve(stype: str, deadline: float) -> list[Resolved]:
    argv = [AVAHI_BROWSE, "--parsable", "--resolve", "--terminate", stype]
    timeout_s = max(0.0, min(AVAHI_BROWSE_TIMEOUT_S, deadline - time.monotonic()))
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, env=_env(),
                              stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ScanTimeout(f"{AVAHI_BROWSE} did not finish within {timeout_s:.1f}s") from exc
    if done.returncode != 0:
        raise MdnsUnavailable(f"{AVAHI_BROWSE} exited {done.returncode}: {done.stderr.strip()[:300]}")
    return parse_avahi_resolved(done.stdout.splitlines(), stype)


def _avahi_publish(argv: list[str]) -> subprocess.Popen[bytes]:
    """Start ``avahi-publish`` and wait until Avahi says the record is live, else raise."""
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=_env())
    assert proc.stderr is not None
    said = b""
    deadline = time.monotonic() + AVAHI_ESTABLISH_S
    while b"Established under name" not in said:
        left = deadline - time.monotonic()
        ready = select.select([proc.stderr], [], [], max(left, 0))[0] if left > 0 else []
        chunk = os.read(proc.stderr.fileno(), 4096) if ready else b""
        if not chunk:
            proc.terminate()
            proc.wait(timeout=2)
            reason = said.decode(errors="replace").strip() or f"no answer within {AVAHI_ESTABLISH_S}s"
            raise MdnsUnavailable(f"{AVAHI_PUBLISH} did not register the invite: {reason[:300]}")
        said += chunk
    return proc
