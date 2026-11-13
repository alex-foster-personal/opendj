"""Art-Net address selection for the queued output implementation.

Configuration belongs to one daemon lifetime and starts unconfigured. Selecting an
address neither persists it nor enables output, configures a destination or sends UDP.
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, StrictInt

from apps.lighting.artnet import MAX_NET, MAX_SUBNET, MAX_UNIVERSE, port_address

ARTNET_ADDRESS_STATE_ATTR = "lighting_artnet_address"


class ArtNetAddressRequest(BaseModel):
    """Required integer address fields; JSON booleans are refused before range checks."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    net: StrictInt
    subnet: StrictInt
    universe: StrictInt


class ArtNetAddress(ArtNetAddressRequest):
    """Validated selection with its derived 15-bit Port-Address; no delivery claim."""

    port_address: int


class ArtNetAddressErrorDetail(BaseModel):
    code: Literal["lighting_artnet_bad_net", "lighting_artnet_bad_subuni"]
    message: str


class ArtNetAddressErrorResponse(BaseModel):
    detail: ArtNetAddressErrorDetail


class LightingDisabledDetail(BaseModel):
    error: Literal["flag_disabled"]
    flag: Literal["lighting.output"]
    message: str


class LightingDisabledResponse(BaseModel):
    detail: LightingDisabledDetail


class ArtNetAddressError(ValueError):
    """Range refusal that the HTTP boundary maps to 400."""

    def __init__(self, detail: ArtNetAddressErrorDetail) -> None:
        super().__init__(detail.message)
        self.detail = detail


def validate_artnet_address(fields: ArtNetAddressRequest) -> ArtNetAddress:
    """Check net first, then the two SubUni fields, without modifying any state."""
    if not 0 <= fields.net <= MAX_NET:
        raise ArtNetAddressError(
            ArtNetAddressErrorDetail(
                code="lighting_artnet_bad_net", message=f"net must be 0..{MAX_NET}, got {fields.net}"
            )
        )
    for name, value, limit in (("subnet", fields.subnet, MAX_SUBNET), ("universe", fields.universe, MAX_UNIVERSE)):
        if not 0 <= value <= limit:
            raise ArtNetAddressError(
                ArtNetAddressErrorDetail(
                    code="lighting_artnet_bad_subuni", message=f"{name} must be 0..{limit}, got {value}"
                )
            )
    return ArtNetAddress(
        net=fields.net,
        subnet=fields.subnet,
        universe=fields.universe,
        port_address=port_address(fields.net, fields.subnet, fields.universe),
    )


def get_artnet_address(app: FastAPI) -> ArtNetAddress | None:
    """Read the last accepted selection, or None before configuration."""
    return getattr(app.state, ARTNET_ADDRESS_STATE_ATTR, None)


def set_artnet_address(app: FastAPI, fields: ArtNetAddressRequest) -> ArtNetAddress:
    """Replace the immutable selection only after every field has passed validation."""
    address = validate_artnet_address(fields)
    setattr(app.state, ARTNET_ADDRESS_STATE_ATTR, address)
    return address
