"""Waveform materialization backend selection (optional Rust PyO3 extension).

Ported from the pre-decomposition ``apps/webui/server/rb_vendor.py`` (commits
16ccad40 / ee5e6667 on main) into its own module during the af--dmg-installer
integration: the T3b split landed on that branch while the native backend
landed on main, and the backend gate is policy, not ANLZ decode, so it gets a
named home instead of growing ``anlz.py`` past the 600-line gate. It moved down
to ``apps.analysis_waveform`` with ``bands.py`` (see that module's docstring).

The selection runs ONCE at import. ``MDT_WAVEFORM_BACKEND`` policy values:

    auto    (default) use the native extension when importable, else the
            exact NumPy fallback in ``bands._bands_payload_python``
    python  never import the extension
    native  fail closed at import when the extension is unavailable

Consumers reach every name here through the ``rb_vendor`` facade.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_WAVEFORM_BACKEND_REQUEST = os.environ.get("MDT_WAVEFORM_BACKEND", "auto").strip().lower()
if _WAVEFORM_BACKEND_REQUEST not in {"auto", "python", "native"}:
    raise RuntimeError(
        "MDT_WAVEFORM_BACKEND must be one of auto, python, or native; "
        f"got {_WAVEFORM_BACKEND_REQUEST!r}"
    )

_WAVEFORM_NATIVE_IMPORT_ERROR: ImportError | None = None
if _WAVEFORM_BACKEND_REQUEST == "python":
    _WAVEFORM_NATIVE = None
else:
    try:
        import _rb_waveform_native as _WAVEFORM_NATIVE
    except ImportError as exc:
        _WAVEFORM_NATIVE_IMPORT_ERROR = exc
        if _WAVEFORM_BACKEND_REQUEST == "native":
            raise RuntimeError(
                "MDT_WAVEFORM_BACKEND=native requested, but the release "
                "waveform extension could not be imported"
            ) from exc
        # auto is the compatibility default: source checkouts, unsupported
        # platforms, and baseline wheels keep the exact NumPy path.
        _WAVEFORM_NATIVE = None
        log.warning(
            "native waveform extension unavailable; using Python/NumPy fallback: %s",
            exc,
        )


def waveform_materialization_backend() -> str:
    """Return the inspectable backend selected once when this module loaded."""
    return "rust-pyo3" if _WAVEFORM_NATIVE is not None else "python-numpy"


def waveform_materialization_backend_request() -> str:
    """Return the validated MDT_WAVEFORM_BACKEND policy value."""
    return _WAVEFORM_BACKEND_REQUEST


def waveform_materialization_status() -> dict[str, Any]:
    """Return the selected backend and any native activation failure."""
    error = _WAVEFORM_NATIVE_IMPORT_ERROR
    return {
        "requested": _WAVEFORM_BACKEND_REQUEST,
        "selected": waveform_materialization_backend(),
        "native_available": _WAVEFORM_NATIVE is not None,
        "native_import_error": (
            f"{type(error).__name__}: {error}" if error is not None else None
        ),
    }
