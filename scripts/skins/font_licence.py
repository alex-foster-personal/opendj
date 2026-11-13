"""Font allowlist and licence check for a skin (PLAN.md 3.2, 3.5, 4.1).

Every ``font.*`` token must name a family that is bundled with the app or provided by a
packaged asset, and every packaged ``.woff2`` asset must declare an open licence.

``tokens`` maps token names (for example ``font.body``) to family names. ``assets`` is a
list of ``{"file", "family", "licence"}`` dicts. Returns a list of error strings; empty means pass.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

ALLOWED_LICENCES = frozenset({"OFL-1.1", "Apache-2.0", "MIT"})


def check_skin_fonts(
    tokens: Mapping[str, str],
    assets: Iterable[Mapping[str, object]],
    bundled_families: Iterable[str],
) -> list[str]:
    errors: list[str] = []
    packaged: set[str] = set()
    for asset in assets:
        file = str(asset.get("file", ""))
        if not file.lower().endswith(".woff2"):
            continue
        licence = asset.get("licence")
        if not isinstance(licence, str) or not licence.strip():
            errors.append(f"{file}: no licence declared")
            continue
        if licence.strip() not in ALLOWED_LICENCES:
            errors.append(f"{file}: licence {licence!r} is not in the open-licence allowlist")
            continue
        family = asset.get("family")
        if isinstance(family, str) and family:
            packaged.add(family)
    allowed = set(bundled_families) | packaged
    for name, family in tokens.items():
        if name.startswith("font.") and family not in allowed:
            errors.append(f"{name}: family {family!r} is neither bundled nor packaged")
    return errors
