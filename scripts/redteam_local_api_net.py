"""Kernel listen-table and raw HTTP helpers for track (g) local-API probes.

Kept beside ``scripts.redteam_local_api`` so neither file crosses the
600-line Python ratchet. Callers still go through ``-m scripts.redteam_local_api``.

-Claude
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
from collections.abc import Mapping, Sequence
from http.client import HTTPConnection, HTTPException, HTTPResponse
from pathlib import Path

LISTEN_STATE = "0A"


def listening_addresses(port: int) -> tuple[str, ...]:
    """Return listen addresses for ``port`` from the kernel table (and ss)."""
    found: list[str] = []
    for address, listen_port in _proc_listeners() + _ss_listeners():
        if listen_port != port:
            continue
        if address not in found:
            found.append(address)
    return tuple(found)


def non_loopback_ipv4() -> tuple[str, ...]:
    """Host IPv4 addresses that are not loopback and not the lo dummy."""
    found: list[str] = []
    for address in _ip_cmd_ipv4() + _outbound_ipv4():
        if address in found or is_loopback_ip(address):
            continue
        found.append(address)
    return tuple(found)


def is_loopback_ip(address: str) -> bool:
    """True only for 127.0.0.0/8, ::1, and the name localhost."""
    host = address.split("%", 1)[0].strip("[]")
    if host in {"*", "localhost"}:
        return host == "localhost"
    try:
        packed = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(packed.is_loopback)


def origin_allowed(origin: str, acao: str | None) -> bool:
    """Whether a CORS ACAO header allows this Origin."""
    if acao is None or acao == "":
        return False
    if acao == "*":
        return True
    return acao == origin


def acao(
    host: str, port: int, path: str, origin: str
) -> tuple[str | None, int | None, str | None]:
    """GET path with Origin and return (ACAO header, status, error)."""
    headers, status, err = http_headers(
        host, port, "GET", path, {"Origin": origin, "Accept": "application/json"}
    )
    if headers is None:
        return None, status, err
    return headers.get("access-control-allow-origin"), status, err


def http_status(
    host: str, port: int, method: str, path: str, timeout: float = 4.0
) -> tuple[int | None, str | None]:
    """HTTP status or (None, error) when the socket cannot be used."""
    headers, status, err = http_headers(host, port, method, path, timeout=timeout)
    if headers is None:
        return None, err
    return status, err


def http_body(
    host: str, port: int, method: str, path: str, body: str | None
) -> tuple[int | None, str | None, str]:
    """Status, error, and a short body preview. Path is sent un-normalized."""
    try:
        conn = HTTPConnection(host, port, timeout=4.0)
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read(240).decode("utf-8", "replace").replace("\n", " ")
        status = response.status
        conn.close()
    except (OSError, HTTPException, TimeoutError) as exc:
        return None, f"{type(exc).__name__}: {exc}", ""
    else:
        return status, None, payload


def http_headers(
    host: str,
    port: int,
    method: str,
    path: str,
    extra: Mapping[str, str] | None = None,
    timeout: float = 4.0,
) -> tuple[dict[str, str] | None, int | None, str | None]:
    """Response headers (lower-cased) plus status, or an error string."""
    try:
        conn = HTTPConnection(host, port, timeout=timeout)
        conn.request(method, path, headers=dict(extra or {}))
        response: HTTPResponse = conn.getresponse()
        payload = {k.lower(): v for k, v in response.getheaders()}
        status = response.status
        response.read()
        conn.close()
    except (OSError, HTTPException, TimeoutError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    else:
        return payload, status, None


def _proc_listeners() -> list[tuple[str, int]]:
    rows: list[tuple[str, int]] = []
    rows.extend(_parse_proc_net(Path("/proc/net/tcp"), ipv6=False))
    rows.extend(_parse_proc_net(Path("/proc/net/tcp6"), ipv6=True))
    return rows


def _parse_proc_net(path: Path, *, ipv6: bool) -> list[tuple[str, int]]:
    if not path.is_file():
        return []
    rows: list[tuple[str, int]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != LISTEN_STATE:
            continue
        try:
            rows.append(_split_hex_addr(parts[1], ipv6=ipv6))
        except ValueError:
            continue
    return rows


def _split_hex_addr(field: str, *, ipv6: bool) -> tuple[str, int]:
    ip_hex, port_hex = field.split(":")
    port = int(port_hex, 16)
    raw = bytes.fromhex(ip_hex)
    if ipv6:
        groups = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
        ip = socket.inet_ntop(socket.AF_INET6, groups)
    else:
        ip = socket.inet_ntoa(raw[::-1])
    return ip, port


def _ss_listeners() -> list[tuple[str, int]]:
    ss = _run_capture(("ss", "-ltnH"))
    if ss is None:
        return []
    rows: list[tuple[str, int]] = []
    for line in ss.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        host, sep, port_s = local.rpartition(":")
        if not sep or not port_s.isdigit():
            continue
        rows.append((host.strip("[]"), int(port_s)))
    return rows


def _ip_cmd_ipv4() -> list[str]:
    text = _run_capture(("ip", "-4", "-o", "addr", "show"))
    if text is None:
        return []
    found: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        iface = parts[1]
        if iface == "lo" or iface.startswith("lo:"):
            continue
        address = parts[3].split("/", 1)[0]
        if address not in found:
            found.append(address)
    return found


def _outbound_ipv4() -> list[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        address = sock.getsockname()[0]
    except OSError:
        return []
    finally:
        sock.close()
    return [address]


def _run_capture(argv: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(
            list(argv), check=False, capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout
