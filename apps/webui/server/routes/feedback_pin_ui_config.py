"""Re-export pin UI config models from the mountable pins package."""

from apps.opendj_pins.http import PIN_UI_CONFIG_MAX_SWITCHES, PinUiConfig

__all__ = ["PIN_UI_CONFIG_MAX_SWITCHES", "PinUiConfig"]
