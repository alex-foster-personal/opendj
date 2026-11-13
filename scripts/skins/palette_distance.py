"""CIEDE2000 role-colour distance between two skin palettes.

``delta_e_2000`` follows Sharma, Wu and Dalal (2005). ``palette_distance`` is the mean
CIEDE2000 over the named roles, comparing ``reference[role]`` with ``candidate[role]``.
Colours are sRGB hex (``#rrggbb`` or ``#rgb``), converted to CIELAB with a D65 white.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping

_HEX = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
_WHITE = (0.95047, 1.0, 1.08883)

Lab = tuple[float, float, float]


class PaletteError(ValueError):
    pass


def parse_hex(value: object, role: str = "") -> tuple[int, int, int]:
    m = _HEX.match(value) if isinstance(value, str) else None
    if not m:
        raise PaletteError(f"role {role!r}: not a 6 or 3 digit hex colour: {value!r}")
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lin(c8: int) -> float:
    c = c8 / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _f(t: float) -> float:
    return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29


def hex_to_lab(value: str, role: str = "") -> Lab:
    r, g, b = (_lin(c) for c in parse_hex(value, role))
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    fx, fy, fz = (_f(v / w) for v, w in zip((x, y, z), _WHITE, strict=True))
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def ciede2000(lab1: Lab, lab2: Lab) -> float:
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    c_bar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(c_bar**7 / (c_bar**7 + 25**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0
    dlp = l2 - l1
    dcp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180:
            dhp -= 360
        elif dhp < -180:
            dhp += 360
    dHp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))
    lbp = (l1 + l2) / 2
    cbp = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbp = (h1p + h2p + 360) / 2
    else:
        hbp = (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hbp - 30))
        + 0.24 * math.cos(math.radians(2 * hbp))
        + 0.32 * math.cos(math.radians(3 * hbp + 6))
        - 0.20 * math.cos(math.radians(4 * hbp - 63))
    )
    d_theta = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(cbp**7 / (cbp**7 + 25**7))
    sl = 1 + 0.015 * (lbp - 50) ** 2 / math.sqrt(20 + (lbp - 50) ** 2)
    sc = 1 + 0.045 * cbp
    sh = 1 + 0.015 * cbp * t
    rt = -math.sin(math.radians(2 * d_theta)) * rc
    return math.sqrt((dlp / sl) ** 2 + (dcp / sc) ** 2 + (dHp / sh) ** 2 + rt * (dcp / sc) * (dHp / sh))


def delta_e_2000(hex_a: str, hex_b: str) -> float:
    return ciede2000(hex_to_lab(hex_a), hex_to_lab(hex_b))


def palette_distance(reference: Mapping[str, str], candidate: Mapping[str, str], roles: Iterable[str]) -> float:
    roles = list(roles)
    if not roles:
        raise PaletteError("no roles given")
    total = 0.0
    for role in roles:
        for name, pal in (("reference", reference), ("candidate", candidate)):
            if role not in pal:
                raise PaletteError(f"role {role!r} missing from {name} palette")
        total += ciede2000(hex_to_lab(reference[role], role), hex_to_lab(candidate[role], role))
    return total / len(roles)
