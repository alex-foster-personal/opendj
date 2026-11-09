"""LIGHT-12: discover OS2L lighting apps on the LAN through the system's DNS-SD client.

OS2L servers (lighting software that takes beat and button messages from DJ software) announce
``_os2l._tcp`` over DNS-SD. python-zeroconf is LGPL, which the license gate refuses, so this
follows the pattern of ``apps/b2b_pairing/b2b_mdns.py`` (not imported: the lighting package
stays independent) and drives the system responder's own client:

- macOS: ``dns-sd -B`` to browse, then ``dns-sd -L`` per instance to resolve host and port.
  ``dns-sd`` streams forever and block-buffers a pipe, so it runs on a pty for a fixed window.
- Linux: ``avahi-browse --parsable --resolve --terminate``, which resolves in one call.

With neither client installed, ``discover_os2l`` raises ``Os2lDiscoveryUnavailable`` naming
both. An instance that resolves to no port is dropped and logged by name, never returned with
port 0. Parsing is pure and tested against recorded CLI output.
"""

from __future__ import annotations

import logging
import os
import re
import select
import shutil
import subprocess
import time
from dataclasses import dataclass

SERVICE_TYPE = "_os2l._tcp"
DNS_SD = "dns-sd"
AVAHI_BROWSE = "avahi-browse"
AF_SERVICE_ID = "com.af.music-dj-tools.lighting-os2l-discovery"
BROWSE_S = 3.0
RESOLVE_S = 2.0
AVAHI_TIMEOUT_S = 10.0
TERM_GRACE_S = 1.0

LOG = logging.getLogger(__name__)

_BROWSE_LINE = re.compile(r"^\s*\S+\s+Add\s+\d+\s+\d+\s+\S+\s+(\S+)\s+(.+?)\s*$")
_RESOLVE_LINE = re.compile(r"^\s*\S+\s+(.+?)\._os2l\._tcp\.\S*\s+can be reached at (\S+?)\.?:(\d+)\b")
_ESCAPE = re.compile(r"\\(\d{3}|.)")


class Os2lDiscoveryUnavailable(RuntimeError):
    """No DNS-SD client is installed, or the one that is could not be used."""


@dataclass(frozen=True)
class Os2lServer:
    """One OS2L server found on the LAN."""

    instance: str
    host: str
    port: int


# -----------------------------------------------------------------------------
# parsing (pure)


def _unescape(text: str) -> str:
    """DNS-SD escapes names: backslash + 3 decimal digits is one byte (032 is a space)."""
    return _ESCAPE.sub(lambda m: chr(int(m.group(1))) if m.group(1).isdigit() else m.group(1), text)


def _server_or_none(instance: str, host: str, port: int) -> Os2lServer | None:
    if port <= 0:
        LOG.warning("OS2L instance %r resolved to no port; dropped", instance)
        return None
    return Os2lServer(instance=instance, host=host, port=port)


def parse_dns_sd_browse(lines: list[str]) -> list[str]:
    """Instance names from ``dns-sd -B _os2l._tcp`` output, in order, without duplicates."""
    names: list[str] = []
    for line in lines:
        match = _BROWSE_LINE.match(line)
        if match and match.group(1).rstrip(".") == SERVICE_TYPE and match.group(2) not in names:
            names.append(match.group(2))
    return names


def parse_dns_sd_resolve(lines: list[str]) -> dict[str, tuple[str, int]]:
    """Instance name -> (host, port) from ``dns-sd -L`` output (first answer per instance)."""
    found: dict[str, tuple[str, int]] = {}
    for line in lines:
        match = _RESOLVE_LINE.match(line)
        if match:
            found.setdefault(_unescape(match.group(1)), (match.group(2), int(match.group(3))))
    return found


def servers_from_dns_sd(browse_lines: list[str], resolve_lines: list[str]) -> list[Os2lServer]:
    """Join browse and resolve output; a browsed instance with no answer or no port is dropped."""
    resolved = parse_dns_sd_resolve(resolve_lines)
    servers: list[Os2lServer] = []
    for name in parse_dns_sd_browse(browse_lines):
        if name not in resolved:
            LOG.warning("OS2L instance %r did not resolve; dropped", name)
            continue
        server = _server_or_none(name, *resolved[name])
        if server is not None:
            servers.append(server)
    return servers


def parse_avahi_resolved(lines: list[str]) -> list[Os2lServer]:
    """``avahi-browse -p -r`` ``=`` lines -> one server per instance (first answer wins).

    Fields: ``=;iface;proto;name;type;domain;host;address;port;txt``.
    """
    servers: dict[str, Os2lServer | None] = {}
    for line in lines:
        parts = line.split(";", 9)
        if len(parts) < 9 or parts[0] != "=" or parts[4] != SERVICE_TYPE:
            continue
        name = _unescape(parts[3])
        if name in servers:
            continue
        port = int(parts[8]) if parts[8].isdigit() else 0
        servers[name] = _server_or_none(name, parts[6], port)
    return [server for server in servers.values() if server is not None]


# -----------------------------------------------------------------------------
# live browse


def available_tool() -> tuple[str, str] | None:
    """``(name, resolved path)`` of ``dns-sd`` or ``avahi-browse``, whichever is on PATH first; None when neither is."""
    for tool in (DNS_SD, AVAHI_BROWSE):
        if resolved := shutil.which(tool):
            return tool, resolved
    return None


def _env() -> dict[str, str]:
    return {**os.environ, "AF_SERVICE_ID": AF_SERVICE_ID}


def _stream_for(argv: list[str], window_s: float) -> list[str]:
    """Run a streaming dns-sd query on a pty for ``window_s`` and return its lines."""
    import pty

    master, slave = pty.openpty()
    proc = subprocess.Popen(argv, stdout=slave, stderr=slave, stdin=subprocess.DEVNULL, env=_env(), close_fds=True)
    os.close(slave)
    buffer = b""
    deadline = time.monotonic() + window_s
    try:
        while (left := deadline - time.monotonic()) > 0:
            if not select.select([master], [], [], left)[0]:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:  # EIO: the child closed its side
                break
            if not chunk:
                break
            buffer += chunk
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        os.close(master)
    return [line.rstrip("\r") for line in buffer.decode(errors="replace").split("\n") if line.strip()]


def _discover_with_dns_sd(exe: str, browse_s: float) -> list[Os2lServer]:
    browse = _stream_for([exe, "-B", SERVICE_TYPE, "local"], browse_s)
    resolve: list[str] = []
    for name in parse_dns_sd_browse(browse):
        resolve += _stream_for([exe, "-L", name, SERVICE_TYPE, "local"], RESOLVE_S)
    return servers_from_dns_sd(browse, resolve)


def _discover_with_avahi(exe: str) -> list[Os2lServer]:
    argv = [exe, "--parsable", "--resolve", "--terminate", SERVICE_TYPE]
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=AVAHI_TIMEOUT_S,
            env=_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise Os2lDiscoveryUnavailable(f"{AVAHI_BROWSE} did not finish within {AVAHI_TIMEOUT_S:.0f}s") from exc
    if done.returncode != 0:
        raise Os2lDiscoveryUnavailable(f"{AVAHI_BROWSE} exited {done.returncode}: {done.stderr.strip()[:300]}")
    return parse_avahi_resolved(done.stdout.splitlines())


def discover_os2l(browse_s: float = BROWSE_S) -> list[Os2lServer]:
    """Every OS2L server answering on the LAN, browsed for ``browse_s`` seconds (dns-sd only)."""
    found = available_tool()
    if found is not None and found[0] == DNS_SD:
        return _discover_with_dns_sd(found[1], browse_s)
    if found is not None and found[0] == AVAHI_BROWSE:
        return _discover_with_avahi(found[1])
    raise Os2lDiscoveryUnavailable(
        f"OS2L discovery needs {DNS_SD} (macOS) or {AVAHI_BROWSE} (Linux, Avahi); neither is on PATH"
    )
