"""Consult ``app.state.feature_flags`` before a USB route touches hardware.

SAND-01/SAND-02. ``GET /api/v1/flags`` already reports ``usb.export``
disabled under the App Store build profile, but a route that never reads
``app.state.feature_flags`` stays callable regardless of what that response
says -- the exact gap SAND-01 exists to close. Every USB route (plan, apply,
readback, volume listing, volume events) reads the SAME store this module
reads through :func:`apps.feature_flags.store_build_refusal`, so the
disclosure and the enforcement cannot drift apart (ENT-02, applied to the
fourth refusal), and a flag disabled for a LOCAL reason is never blamed on
Apple's sandbox (SAND-01 review round 2, PR #1668).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from apps.feature_flags import (
    FlagRefusal,
    FlagStore,
    store_build_refusal,
    store_profile_is_source,
)
from apps.shared.sandbox import is_sandboxed

USB_EXPORT_FLAG: str = "usb.export"


@dataclass(frozen=True)
class UsbExportGate:
    """The resolved decision for one request: is the capability usable?"""

    #: The flag's own resolved value -- a CONFIG fact.
    flag_enabled: bool
    #: Non-None when the reason is attributable to the store profile or to
    #: an actually-sandboxed runtime (SAND-04); None for a plain local
    #: override, which must not carry Apple's name.
    refusal: FlagRefusal | None

    @property
    def available(self) -> bool:
        """Callable this request: the flag is on AND nothing refuses it.

        ``flag_enabled`` alone is not enough: a mis-packaged bundle can ship
        the full profile (flag reads on) while genuinely running inside the
        sandbox, and ``refusal`` is what carries that runtime fact.
        """
        return self.flag_enabled and self.refusal is None


def _flag_store(request: Request) -> FlagStore:
    """Fail fast rather than defaulting to "enabled" when the store is not
    mounted: a store build whose boot sequence failed to wire its FlagStore
    must not silently serve USB wide open. ``apps.webui.server.app.create_app``
    always wires one (see its ``feature_flags`` parameter).
    """
    store: FlagStore | None = getattr(request.app.state, "feature_flags", None)
    if store is None:
        raise RuntimeError(
            "feature flags are not mounted on app.state.feature_flags; a USB "
            "route cannot be gated without reading the flag it is gated on. "
            "Wire app.state.feature_flags at boot (see "
            "apps.webui.server.app.create_app) before serving this route."
        )
    return store


def usb_export_gate(request: Request) -> UsbExportGate:
    """Read ``usb.export`` from the boot-time store and resolve its refusal."""
    store = _flag_store(request)
    flag = store.state(USB_EXPORT_FLAG)
    refusal = store_build_refusal(
        flag,
        from_store_profile=store_profile_is_source(store),
        sandboxed=is_sandboxed(),
    )
    return UsbExportGate(flag_enabled=flag.enabled, refusal=refusal)


__all__ = ["USB_EXPORT_FLAG", "UsbExportGate", "usb_export_gate"]
