"""UTC timestamp helper used by webui server modules."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from apps.webui.server.utc_timestamps import utc_now_iso_ms

_ISO_MS_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def test_utc_now_iso_ms_matches_millisecond_z_suffix() -> None:
    stamp = utc_now_iso_ms()
    assert _ISO_MS_Z.match(stamp)
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    # ``isoformat(timespec="milliseconds")`` truncates sub-ms; compare at ms.
    now_ms = datetime.now(UTC).replace(microsecond=0)
    assert abs((parsed - now_ms).total_seconds()) < 2.0
