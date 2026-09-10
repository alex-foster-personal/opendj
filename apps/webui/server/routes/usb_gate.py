"""Consult ``app.state.feature_flags`` before a USB route touches hardware.

SAND-01/SAND-02. ``GET /api/v1/flags`` already reports ``usb.export``
disabled under the App Store build profile, but a route that never reads
``app.state.feature_flags`` stays callable regardless of what that response
says -- the exact gap SAND-01 exists to close. Every USB route (plan, apply,
readback, volume listing, volume events) reads the SAME store this module
reads, so the disclosure and the enforcement cannot drift apart (ENT-02,
applied to the fourth refusal).
"""

from __future__ import annotations

from fastapi import Request

from apps.feature_flags import FlagStore

USB_EXPORT_FLAG: str = "usb.export"


def usb_export_enabled(request: Request) -> bool:
    """Is ``usb.export`` on for this process, read from the boot-time store?

    Fails fast rather than defaulting to "enabled" when the store is not
    mounted: a store build whose boot sequence failed to wire its FlagStore
    must not silently serve USB wide open. Production always wires it (see
    ``apps.engine_core.app.create_app``); a bare legacy app that never
    mounts one is a test or integration bug, not a build to fail open for.
    """
    store: FlagStore | None = getattr(request.app.state, "feature_flags", None)
    if store is None:
        raise RuntimeError(
            "feature flags are not mounted on app.state.feature_flags; a USB "
            "route cannot be gated without reading the flag it is gated on. "
            "Wire app.state.feature_flags at boot (see "
            "apps.engine_core.app.create_app) before serving this route."
        )
    return store.enabled(USB_EXPORT_FLAG)


__all__ = ["USB_EXPORT_FLAG", "usb_export_enabled"]
