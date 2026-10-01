"""The closed UI configuration snapshot a feedback pin may carry (pin 49f9d217).

Split out of routes/feedback.py, which uses it in the pin create body and in
the stored pin environment.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

PIN_UI_CONFIG_MAX_SWITCHES = 24
_PIN_SLUG = r"^[a-z0-9][a-z0-9_-]{0,31}$"
_PIN_SWITCH_KEY = re.compile(r"^[a-z][a-z0-9_.]{0,47}$")
_PIN_ROUTE = r"^/[A-Za-z0-9/_.-]{0,119}$"
_PIN_ROUTE_HOME_PREFIXES = ("/users/", "/home/")


class PinUiConfig(BaseModel):
    """Compact UI configuration at the moment a pin was dropped (pin 49f9d217).

    Closed by construction: unknown keys are refused, every mode is a short
    lowercase slug, every switch is a boolean, and the route is an app route
    with no query string. That is what keeps a secret, a path under the
    user's home, or a track title out of the pin store: there is no field
    free text could travel in.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: str = Field(pattern=_PIN_ROUTE)
    app_mode: str = Field(pattern=_PIN_SLUG)
    engine_mode: str = Field(pattern=_PIN_SLUG)
    perf_tier: str = Field(pattern=_PIN_SLUG)
    switches: dict[str, StrictBool]

    @field_validator("route")
    @classmethod
    def _route_is_not_a_home_path(cls, value: str) -> str:
        if value.lower().startswith(_PIN_ROUTE_HOME_PREFIXES):
            raise ValueError("route must be an app route, not a filesystem path")
        return value

    @field_validator("switches")
    @classmethod
    def _switches_are_bounded_slugs(cls, value: dict[str, bool]) -> dict[str, bool]:
        if len(value) > PIN_UI_CONFIG_MAX_SWITCHES:
            raise ValueError(
                f"at most {PIN_UI_CONFIG_MAX_SWITCHES} switches, got {len(value)}"
            )
        for key in value:
            if _PIN_SWITCH_KEY.fullmatch(key) is None:
                raise ValueError(f"switch name {key!r} is not a lowercase identifier")
        return value


__all__ = ["PIN_UI_CONFIG_MAX_SWITCHES", "PinUiConfig"]
