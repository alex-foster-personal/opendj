"""Parse ``vmmap`` / ``sysctl`` style byte counts such as ``1.5M`` or ``512``.

INTERPRETER FLOOR: imported by ``apps.diagnostics.probe_types`` and
``apps.shared.machine_metrics``, which run under ``/usr/bin/python3`` (3.9 on
current macOS). Stdlib-only; no 3.10+/3.11+ runtime syntax; annotations are
deferred.

``scripts/diagnostics/probe_types.py`` keeps its own copy of this parser on
purpose: it is copied verbatim into the launchd probe payload, where ``apps``
cannot be imported (guarded by
``tests/scripts/test_opendj_performance_probe_install.py``).
"""

from __future__ import annotations

import re

_MIB = 1024 * 1024
_BYTE_COUNT_PATTERN = re.compile(r"([0-9.]+)([KMGTP])?", re.IGNORECASE)
_BYTE_UNIT_SCALE: dict[str, int] = {
    "": 1,
    "K": 1024,
    "M": _MIB,
    "G": 1024 * _MIB,
    "T": 1024**4,
    "P": 1024**5,
}


def parse_byte_count(raw: str) -> int | None:
    match = _BYTE_COUNT_PATTERN.fullmatch(raw.strip())
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "").upper()
    return int(value * _BYTE_UNIT_SCALE[unit])
