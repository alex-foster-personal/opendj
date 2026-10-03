"""SPDX-ish license expression classification for the third-party inventory.

Split out of scripts/third_party_licenses.py (file_size.over_limit_python
ratchet, PR #4853): this half is self-contained -- the category model, the
marker lists and classify_license() -- and carries no payload/filesystem
dependencies, unlike the rest of that module.
"""

from __future__ import annotations

import re


class Cat:
    PERMISSIVE = "permissive"
    WEAK = "weak-copyleft"
    STRONG = "strong-copyleft"
    NONCOM = "non-commercial"
    UNKNOWN = "unknown"


#: Public: scripts/third_party_licenses.py sorts the flag report by this rank.
RANK = {Cat.PERMISSIVE: 0, Cat.WEAK: 1, Cat.STRONG: 2, Cat.NONCOM: 3, Cat.UNKNOWN: 4}
FLAGGED = (Cat.WEAK, Cat.STRONG, Cat.NONCOM, Cat.UNKNOWN)

_PERMISSIVE_MARKERS = (
    "mit", "mit-0", "bsd", "bsd-2-clause", "bsd-3-clause", "apache", "apache-2.0", "isc", "iscl",
    "psf", "psf-2.0", "python-2.0", "cnri-python", "zlib", "unlicense", "cc0-1.0", "0bsd", "hpnd",
    "bsl-1.0", "boost", "public domain", "ofl", "ofl-1.1", "openssl", "curl", "unicode", "blueoak-1.0.0",
    "wtfpl", "zpl-2.1", "python software foundation", "historical", "ncsa", "pil", "libpng", "x11",
    "cc-by-4.0", "cc-by-3.0", "afl-2.1", "artistic-2.0", "llvm-exception",
)
_STRONG_MARKERS = ("agpl", "sspl", "eupl", "osl-3")
_NONCOM_MARKERS = ("cc-by-nc", "cc-by-nc-sa", "cc-by-nc-nd", "non-commercial")


def _has_marker(lowered: str, markers: tuple[str, ...]) -> bool:
    """Whole-token match, so "mit" never fires inside "permit" or "limited"."""
    return any(re.search(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", lowered) for marker in markers)


def _token_category(token: str) -> str:
    lowered = token.strip().lower().strip("()")
    if not lowered:
        return Cat.UNKNOWN
    if _has_marker(lowered, _NONCOM_MARKERS) or "noncommercial" in lowered:
        return Cat.NONCOM
    if "lgpl" in lowered or "lesser" in lowered:
        return Cat.WEAK
    if "gpl" in lowered or "gnu general public" in lowered or _has_marker(lowered, _STRONG_MARKERS):
        return Cat.STRONG
    if any(
        marker in lowered
        for marker in ("mpl", "mozilla", "epl", "epl-1.0", "epl-2.0", "eclipse", "cddl", "cecill-c")
    ):
        return Cat.WEAK
    if _has_marker(lowered, _PERMISSIVE_MARKERS):
        return Cat.PERMISSIVE
    return Cat.UNKNOWN


def classify_license(expression: str) -> str:
    """Category of an SPDX-ish expression.

    ``A OR B`` takes the BEST alternative (the licensee may choose); ``A AND
    B`` and ``A / B`` style joins take the WORST (all apply). Free text that
    matches nothing is UNKNOWN, never assumed permissive.
    """
    alternatives = re.split(r"\s+OR\s+|\s*\|\s*|;\s*", expression.strip(), flags=re.IGNORECASE)
    ranked: list[str] = []
    for alternative in alternatives:
        # "X WITH exception" is still X (an exception only loosens it).
        without_exceptions = re.sub(r"\s+WITH\s+\S+", "", alternative, flags=re.IGNORECASE)
        parts = re.split(r"\s+AND\s+|\s*/\s*", without_exceptions, flags=re.IGNORECASE)
        ranked.append(max((_token_category(part) for part in parts), key=RANK.__getitem__))
    return min(ranked, key=RANK.__getitem__)
