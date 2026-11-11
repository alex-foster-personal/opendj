"""LIGHT-11792: Art-Net 7:4:4 address packing and HTTP configuration.

[if] an Art-Net address is invalid [then] reject it without mutation, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.feature_flags import load_flags
from apps.lighting.artnet import encode_artdmx, port_address, unpack_port_address
from apps.lighting.contract import LIGHTING_FLAG_ID
from apps.lighting.http import router
from apps.lighting.monitor import decode_artdmx
from apps.webui.server.app_wiring import _mount_api_routers

pytestmark = [pytest.mark.requirement("LIGHT-11792")]

ADDRESS_PATH = "/api/v1/lighting/output/artnet/address"
VALID_ADDRESS = {"net": 1, "subnet": 2, "universe": 3}


def _app(tmp_path: Path, *, enabled: bool = True, production_mount: bool = False) -> FastAPI:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "feature-flags.json").write_text(
        json.dumps({LIGHTING_FLAG_ID: enabled}), encoding="utf-8", newline="\n"
    )
    app = FastAPI()
    app.state.feature_flags = load_flags(tmp_path)
    if production_mount:
        app.state.usb_simulation_enabled = False
        _mount_api_routers(app)
    else:
        app.include_router(router, prefix="/api/v1")
    return app


def test_legal_grid_round_trips() -> None:
    for net in range(128):
        for subnet in range(16):
            for universe in range(16):
                address = port_address(net, subnet, universe)
                assert address == (net << 8) | (subnet << 4) | universe
                assert unpack_port_address(address) == (net, subnet, universe)
                assert port_address(*unpack_port_address(address)) == address


@pytest.mark.parametrize(
    ("fields", "address", "wire_bytes"),
    [((0, 0, 1), 1, b"\x01\x00"), ((1, 2, 3), 0x123, b"\x23\x01"), ((127, 15, 15), 0x7FFF, b"\xff\x7f")],
)
def test_real_packet_address(fields: tuple[int, int, int], address: int, wire_bytes: bytes) -> None:
    assert port_address(*fields) == address
    slots = b"\x11\x22"
    packet = encode_artdmx(port_address(*fields), slots, sequence=1)
    assert packet[14:16] == wire_bytes
    decoded = decode_artdmx(packet)
    assert decoded.universe == address
    assert decoded.slots == slots


@pytest.mark.parametrize("field,invalid", [(0, -1), (0, 128), (1, -1), (1, 16), (2, -1), (2, 16)])
def test_pack_refuses_out_of_range_fields(field: int, invalid: int) -> None:
    fields = [0, 0, 1]
    fields[field] = invalid
    with pytest.raises(ValueError):
        port_address(*fields)


@pytest.mark.parametrize("invalid", [True, False, 1.0, "1", None])
def test_helpers_refuse_non_integer_fields(invalid: object) -> None:
    for field in range(3):
        fields = [0, 0, 1]
        fields[field] = cast(int, invalid)
        with pytest.raises(ValueError, match="integer"):
            port_address(*fields)
    with pytest.raises(ValueError, match="integer"):
        unpack_port_address(cast(int, invalid))


@pytest.mark.parametrize("address", [-1, 0x8000])
def test_unpack_refuses_invalid_address(address: int) -> None:
    with pytest.raises(ValueError):
        unpack_port_address(address)


def test_put_and_get_address_are_application_scoped(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path / "first")) as client:
        initial = client.get(ADDRESS_PATH)
        assert initial.status_code == 200
        assert initial.json() is None
        for fields in (
            VALID_ADDRESS,
            VALID_ADDRESS,
            {"net": 127, "subnet": 15, "universe": 15},
            {"net": 0, "subnet": 0, "universe": 0},
        ):
            expected = {**fields, "port_address": port_address(**fields)}
            response = client.put(ADDRESS_PATH, json=fields)
            assert response.status_code == 200
            assert response.json() == expected
            observed = client.get(ADDRESS_PATH)
            assert observed.status_code == 200
            assert observed.json() == expected
        with TestClient(_app(tmp_path / "second")) as other:
            assert other.get(ADDRESS_PATH).json() is None


@pytest.mark.parametrize(
    "fields,code",
    [
        ({"net": 128}, "lighting_artnet_bad_net"),
        ({"net": -1}, "lighting_artnet_bad_net"),
        ({"subnet": 16}, "lighting_artnet_bad_subuni"),
        ({"subnet": -1}, "lighting_artnet_bad_subuni"),
        ({"universe": 16}, "lighting_artnet_bad_subuni"),
        ({"universe": -1}, "lighting_artnet_bad_subuni"),
        ({"net": 128, "subnet": 16, "universe": 16}, "lighting_artnet_bad_net"),
    ],
)
def test_invalid_put_is_400_and_preserves_address(tmp_path: Path, fields: dict[str, int], code: str) -> None:
    with TestClient(_app(tmp_path)) as client:
        accepted = client.put(ADDRESS_PATH, json=VALID_ADDRESS)
        assert accepted.status_code == 200
        response = client.put(ADDRESS_PATH, json={**VALID_ADDRESS, **fields})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == code
        assert response.json()["detail"]["message"]
        assert client.get(ADDRESS_PATH).json() == accepted.json()


def test_flag_off_refuses_get_and_put_without_storing(tmp_path: Path) -> None:
    app = _app(tmp_path, enabled=False)
    with TestClient(app) as client:
        for response in (client.get(ADDRESS_PATH), client.put(ADDRESS_PATH, json=VALID_ADDRESS)):
            assert response.status_code == 409
            assert response.json()["detail"]["flag"] == LIGHTING_FLAG_ID
        assert not hasattr(app.state, "lighting_artnet_address")
        app.state.feature_flags = _app(tmp_path / "enabled").state.feature_flags
        assert client.get(ADDRESS_PATH).json() is None


@pytest.mark.parametrize("field", ["net", "subnet", "universe"])
@pytest.mark.parametrize("invalid", [True, False, 1.0, "1", None])
def test_put_requires_strict_integers(tmp_path: Path, field: str, invalid: object) -> None:
    with TestClient(_app(tmp_path)) as client:
        response = client.put(ADDRESS_PATH, json={**VALID_ADDRESS, field: invalid})
        assert response.status_code == 422
        assert client.get(ADDRESS_PATH).json() is None


@pytest.mark.parametrize("fields", [{"net": 1, "subnet": 2}, {**VALID_ADDRESS, "port_address": 0x123}])
def test_put_requires_exact_fields(tmp_path: Path, fields: dict[str, int]) -> None:
    with TestClient(_app(tmp_path)) as client:
        assert client.put(ADDRESS_PATH, json=fields).status_code == 422
        assert client.get(ADDRESS_PATH).json() is None


def test_production_mount_and_openapi_contract(tmp_path: Path) -> None:
    app = _app(tmp_path, production_mount=True)
    with TestClient(app) as client:
        response = client.put(ADDRESS_PATH, json=VALID_ADDRESS)
        assert response.status_code == 200
        assert client.get(ADDRESS_PATH).json() == response.json()
    schema = app.openapi()
    operations = schema["paths"][ADDRESS_PATH]
    assert {"get", "put"} <= operations.keys()
    assert operations["put"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ArtNetAddressRequest"
    )
    for method in ("get", "put"):
        for status in ("200", "409"):
            assert "schema" in operations[method]["responses"][status]["content"]["application/json"]
    assert "schema" in operations["put"]["responses"]["400"]["content"]["application/json"]
