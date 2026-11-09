"""REMOTE-CTL-10613: LAN remote-control listener (issue #10432).

[if] the remote-control listener contracts are exercised [then] tests prove pairing, ping, and hash-only storage, [else stop].
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from apps.webui.server.remote_control import (
    build_listener_app,
    MAX_WRONG_CODES,
    PAIR_PATH,
    PING_PATH,
    RemoteSession,
    devices_file_is_owner_only,
)

pytestmark = [
    pytest.mark.requirement("REMOTE-CTL-10613"),
    pytest.mark.requirement("REMOTE-CTL-10630"),
]


def _port_refuses(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        if int(probe.getsockname()[1]) == port:
            return True
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


async def _open_session(state_dir: Path) -> RemoteSession:
    session = RemoteSession(state_dir=state_dir, requested_port=0)
    await session.open()
    return session


def test_right_code_pairs_ping_and_rotates_code(state_dir: Path) -> None:
    """[if] the right code is posted with a device name [then] 201, ping 200, new code, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        old_code = session.display_code
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                paired = await lan.post(
                    _url(port, PAIR_PATH),
                    json={"code": old_code, "device_name": "the maintainer phone"},
                    headers={"host": f"127.0.0.1:{port}"},
                )
                assert paired.status_code == 201, paired.text
                body = paired.json()
                assert len(body["device_id"]) == 16
                assert body["device_name"] == "the maintainer phone"
                assert body["token"]
                ping = await lan.get(
                    _url(port, PING_PATH),
                    headers={"host": f"127.0.0.1:{port}", "authorization": f"Bearer {body['token']}"},
                )
                assert ping.status_code == 200, ping.text
                assert ping.json() == {"device_id": body["device_id"], "device_name": "the maintainer phone"}
                assert session.display_code != old_code
        finally:
            await session.close()

    asyncio.run(run())


def test_wrong_code_counts_and_fifth_closes_listener(state_dir: Path) -> None:
    """[if] a wrong code is posted [then] 403 wrong_code; fifth closes the listener, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                for attempt in range(1, MAX_WRONG_CODES):
                    refused = await lan.post(
                        _url(port, PAIR_PATH),
                        json={"code": "BAD-C0D", "device_name": "nope"},
                        headers={"host": f"127.0.0.1:{port}"},
                    )
                    assert refused.status_code == 403, refused.text
                    payload = refused.json()
                    assert payload["detail"] == "wrong_code"
                    assert payload["tries_left"] == MAX_WRONG_CODES - attempt
                assert not _port_refuses(port)
                last = await lan.post(
                    _url(port, PAIR_PATH),
                    json={"code": "BAD-C0D", "device_name": "nope"},
                    headers={"host": f"127.0.0.1:{port}"},
                )
                assert last.status_code == 403
                for _ in range(100):
                    if _port_refuses(port):
                        break
                    await asyncio.sleep(0.05)
                assert _port_refuses(port)
                assert session.status == "locked_out"
        finally:
            await session.close()

    asyncio.run(run())


def test_ping_without_token_is_401(state_dir: Path) -> None:
    """[if] the token is missing on /remote/ping [then] 401, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                missing = await lan.get(_url(port, PING_PATH), headers={"host": f"127.0.0.1:{port}"})
                assert missing.status_code == 401
        finally:
            await session.close()

    asyncio.run(run())


def test_ping_with_wrong_token_is_401(state_dir: Path) -> None:
    """[if] the token is wrong on /remote/ping [then] 401, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                bad = await lan.get(
                    _url(port, PING_PATH),
                    headers={"host": f"127.0.0.1:{port}", "authorization": "Bearer not-a-real-token"},
                )
                assert bad.status_code == 401
        finally:
            await session.close()

    asyncio.run(run())


def test_ping_after_forget_is_401(state_dir: Path) -> None:
    """[if] the device was forgotten [then] its token gets 401 on /remote/ping, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        code = session.display_code
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                paired = await lan.post(
                    _url(port, PAIR_PATH),
                    json={"code": code, "device_name": "temp"},
                    headers={"host": f"127.0.0.1:{port}"},
                )
                assert paired.status_code == 201
                token = paired.json()["token"]
                device_id = paired.json()["device_id"]
                session.forget(device_id)
                gone = await lan.get(
                    _url(port, PING_PATH),
                    headers={"host": f"127.0.0.1:{port}", "authorization": f"Bearer {token}"},
                )
                assert gone.status_code == 401
        finally:
            await session.close()

    asyncio.run(run())


def test_host_name_or_foreign_origin_refused_before_parse(state_dir: Path) -> None:
    """[if] Host is a name or Origin is foreign [then] 403 before parsing, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                named = await lan.post(
                    _url(port, PAIR_PATH),
                    json={"code": session.display_code, "device_name": "x"},
                    headers={"host": "localhost"},
                )
                assert named.status_code == 403
                foreign = await lan.post(
                    _url(port, PAIR_PATH),
                    json={"code": session.display_code, "device_name": "x"},
                    headers={
                        "host": f"127.0.0.1:{port}",
                        "origin": "http://evil.example:1",
                    },
                )
                assert foreign.status_code == 403
        finally:
            await session.close()

    asyncio.run(run())


def test_devices_file_stores_hashes_not_tokens(state_dir: Path) -> None:
    """[if] the devices file is read back [then] sha256 hashes only and mode 0600 on POSIX, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        code = session.display_code
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                paired = await lan.post(
                    _url(port, PAIR_PATH),
                    json={"code": code, "device_name": "stored"},
                    headers={"host": f"127.0.0.1:{port}"},
                )
                assert paired.status_code == 201
            path = session.devices_path
            assert path.is_file()
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            devices = on_disk.get("devices")
            assert isinstance(devices, dict) and devices
            for row in devices.values():
                assert isinstance(row, dict)
                assert "token" not in row
                assert isinstance(row.get("token_sha256"), str) and row["token_sha256"]
            assert devices_file_is_owner_only(path)
            if os.name != "nt":
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
        finally:
            await session.close()

    asyncio.run(run())


def test_non_lan_client_is_refused_before_pairing(state_dir: Path) -> None:
    """[if] the client is a global address [then] 403 before pairing, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        try:
            app = build_listener_app(session, f"http://127.0.0.1:{port}")
            with TestClient(app, client=("8.8.8.8", 50123)) as remote:
                refused = remote.post(
                    PAIR_PATH,
                    json={"code": session.display_code, "device_name": "remote"},
                    headers={"host": f"127.0.0.1:{port}"},
                )
                assert refused.status_code == 403, refused.text
        finally:
            await session.close()

    asyncio.run(run())


def test_unlisted_listener_path_returns_404(state_dir: Path) -> None:
    """[if] any path besides pair POST and ping GET is asked [then] 404, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                assert (await lan.get(_url(port, "/docs"), headers={"host": f"127.0.0.1:{port}"})).status_code == 404
                assert (await lan.get(_url(port, PAIR_PATH), headers={"host": f"127.0.0.1:{port}"})).status_code == 404
        finally:
            await session.close()

    asyncio.run(run())
