"""Frozen copy of the pre-#383 ad-hoc Apple bookmark scanner from equivalence.

Source commit: d63e18037b6c396459cc34130a1f1c755ddcabd3
Purpose: parity witness for issue #381 / MIK-AUDIT section 3
Not imported by production code.
"""
from __future__ import annotations

import struct
import unicodedata

_BOOKMARK_STRING = 0x0101
_BOOKMARK_MAGIC = b"book"


def _bookmark_strings(blob: bytes) -> list[str]:
    found: list[str] = []
    size = len(blob)
    offset = 0
    while offset + 8 <= size:
        length, rtype = struct.unpack_from("<II", blob, offset)
        if rtype == _BOOKMARK_STRING and 0 < length <= size - offset - 8:
            try:  # noqa: SIM105 — frozen witness matches d63e180 try/except/pass
                found.append(blob[offset + 8 : offset + 8 + length].decode("utf-8"))
            except UnicodeDecodeError:
                pass  # not a UTF-8 record after all; keep walking
        offset += 4
    return found


def legacy_decode_bookmark_path(blob: bytes | None) -> str | None:
    """Absolute POSIX path inside an Apple bookmark blob, or None.

    Ad-hoc ``0x0101`` scan from equivalence ``sources.py`` before PR #383.
    """
    if not blob or _BOOKMARK_MAGIC not in blob[:64]:
        return None
    records = _bookmark_strings(blob)
    absolute = [s for s in records if s.startswith("/") and len(s) > 1]
    if absolute:
        return unicodedata.normalize("NFC", max(absolute, key=len))
    components = [
        s
        for s in records
        if s and "/" not in s and not s.startswith("NSURL") and s != "Macintosh HD"
    ]
    if not components:
        return None
    return unicodedata.normalize("NFC", "/" + "/".join(components))
