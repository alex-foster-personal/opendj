"""LIGHT-11792: generated OpenAPI contract for the Art-Net address route.

[if] the address route schema regresses [then] fail before clients lose 400/409, [else stop].
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from apps.lighting.contract import LIGHTING_FLAG_ID
from apps.lighting.output_api import ArtNetAddressErrorResponse, LightingDisabledResponse

from .test_artnet_address import _app

pytestmark = [pytest.mark.requirement("LIGHT-11792")]

ADDRESS_PATH = "/api/v1/lighting/output/artnet/address"
REF_PREFIX = "#/components/schemas/"


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Follow a local components $ref to its schema object."""
    ref = node["$ref"]
    assert ref.startswith(REF_PREFIX)
    return cast(dict[str, Any], schema["components"]["schemas"][ref.removeprefix(REF_PREFIX)])


def _response_schema(operation: dict[str, Any], status: str) -> dict[str, Any]:
    """Return the JSON schema node declared for one response status."""
    return cast(dict[str, Any], operation["responses"][status]["content"]["application/json"]["schema"])


def _assert_address_contract(schema: dict[str, Any]) -> None:
    """Assert success, invalid-range and disabled-output schemas are declared and typed."""
    operations = schema["paths"][ADDRESS_PATH]
    assert {"get", "put"} <= operations.keys()
    get_op, put_op = operations["get"], operations["put"]

    request = _resolve(schema, put_op["requestBody"]["content"]["application/json"]["schema"])
    assert request["required"] == ["net", "subnet", "universe"]
    assert request["additionalProperties"] is False

    success = _resolve(schema, _response_schema(put_op, "200"))
    assert set(success["required"]) == {"net", "subnet", "universe", "port_address"}
    assert all(success["properties"][key]["type"] == "integer" for key in success["required"])
    get_success = _response_schema(get_op, "200")["anyOf"]
    assert {"type": "null"} in get_success
    assert any(option.get("$ref") == f"{REF_PREFIX}ArtNetAddress" for option in get_success)

    assert "400" not in get_op["responses"]
    bad_range = _resolve(schema, _response_schema(put_op, "400"))
    assert bad_range["title"] == "ArtNetAddressErrorResponse"
    assert bad_range["required"] == ["detail"]
    bad_detail = _resolve(schema, bad_range["properties"]["detail"])
    assert set(bad_detail["required"]) == {"code", "message"}
    assert set(bad_detail["properties"]["code"]["enum"]) == {"lighting_artnet_bad_net", "lighting_artnet_bad_subuni"}

    for operation in (get_op, put_op):
        disabled = _resolve(schema, _response_schema(operation, "409"))
        assert disabled["title"] == "LightingDisabledResponse"
        disabled_detail = _resolve(schema, disabled["properties"]["detail"])
        assert {"error", "flag", "message"} <= set(disabled_detail["required"])
        assert disabled_detail["properties"]["flag"]["const"] == LIGHTING_FLAG_ID


def test_live_openapi_declares_address_response_schemas(tmp_path: Path) -> None:
    """The production-mounted app documents typed 200, 400 and 409 schemas for the route."""
    _assert_address_contract(_app(tmp_path, production_mount=True).openapi())


def test_runtime_error_bodies_match_declared_schemas(tmp_path: Path) -> None:
    """Real 400 and 409 bodies validate against the models the OpenAPI schema advertises."""
    with TestClient(_app(tmp_path / "on", production_mount=True)) as client:
        bad = client.put(ADDRESS_PATH, json={"net": 128, "subnet": 0, "universe": 1})
        assert bad.status_code == 400
        assert ArtNetAddressErrorResponse.model_validate(bad.json()).detail.code == "lighting_artnet_bad_net"
    with TestClient(_app(tmp_path / "off", enabled=False, production_mount=True)) as client:
        for response in (
            client.get(ADDRESS_PATH),
            client.put(ADDRESS_PATH, json={"net": 0, "subnet": 0, "universe": 1}),
        ):
            assert response.status_code == 409
            assert LightingDisabledResponse.model_validate(response.json()).detail.flag == LIGHTING_FLAG_ID
