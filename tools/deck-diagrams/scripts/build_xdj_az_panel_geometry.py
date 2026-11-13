from __future__ import annotations

from build_xdj_az_panel_layout import _STRIP_ORDER
from build_xdj_az_panel_trace import (
    MM_PER_PX,
    PANEL_PX,
    Px,
    RIGHT_DECK_DX_PX,
    _GLOBAL_PX,
    _LEFT_DECK_PX,
    _RIGHT_DECK_CHECK_PX,
    _strip_px,
)

# ----------------------------------------------------------------- geometry helpers


def _shift_px(box: Px, dx: int) -> Px:
    x0, y0, x1, y1 = box
    return (x0 + dx, y0, x1 + dx, y1)


def px_to_mm(box: Px) -> tuple[float, float, float, float]:
    """Raw conversion of an inclusive pixel box to a millimetre rectangle (0.1 mm)."""
    x0, y0, x1, y1 = box
    sx, sy = MM_PER_PX["x"], MM_PER_PX["y"]
    x = (x0 - PANEL_PX["x0"]) * sx
    y = (y0 - PANEL_PX["y0"]) * sy
    return (
        round(x, 1),
        round(y, 1),
        round((x1 - x0 + 1) * sx, 1),
        round((y1 - y0 + 1) * sy, 1),
    )


def _half(v: float) -> float:
    return round(v * 2) / 2


def _normalise(
    raw: dict[str, tuple[float, float, float, float]],
    rows: list[list[str]],
    cols: list[list[str]],
) -> dict[str, tuple[float, float, float, float]]:
    """Renderer normalisation: whole-mm sizes, 0.5 mm centres, mates on one line."""
    size = {k: (max(1.0, round(w)), max(1.0, round(h))) for k, (_, _, w, h) in raw.items()}
    cx = {k: x + w / 2 for k, (x, _, w, _) in raw.items()}
    cy = {k: y + h / 2 for k, (_, y, _, h) in raw.items()}
    for group in rows:
        mean = sum(cy[k] for k in group) / len(group)
        for k in group:
            cy[k] = mean
    for group in cols:
        mean = sum(cx[k] for k in group) / len(group)
        for k in group:
            cx[k] = mean
    out: dict[str, tuple[float, float, float, float]] = {}
    for k, (w, h) in size.items():
        out[k] = (round(_half(cx[k]) - w / 2, 1), round(_half(cy[k]) - h / 2, 1), w, h)
    return out


def _deck_px(deck: int) -> dict[str, Px]:
    dx = 0 if deck == 1 else RIGHT_DECK_DX_PX
    return {f"d{deck}-{k}": _shift_px(v, dx) for k, v in _LEFT_DECK_PX.items()}


def _deck_groups(deck: int, groups: list[list[str]]) -> list[list[str]]:
    return [[f"d{deck}-{k}" for k in g] for g in groups]


def all_px() -> dict[str, Px]:
    """Every control's traced pixel box, in output order."""
    out: dict[str, Px] = {}
    out.update(_deck_px(1))
    out.update(_deck_px(2))
    for ch in _STRIP_ORDER:
        out.update(_strip_px(ch))
    out.update(_GLOBAL_PX)
    return out


def repeat_spread_mm() -> float:
    """Largest centre disagreement between the translated left-deck trace and the
    independent right-deck reading, in millimetres."""
    worst = 0.0
    for k, box in _RIGHT_DECK_CHECK_PX.items():
        ref = _shift_px(_LEFT_DECK_PX[k], RIGHT_DECK_DX_PX)
        dx = ((box[0] + box[2]) - (ref[0] + ref[2])) / 2 * MM_PER_PX["x"]
        dy = ((box[1] + box[3]) - (ref[1] + ref[3])) / 2 * MM_PER_PX["y"]
        worst = max(worst, (dx * dx + dy * dy) ** 0.5)
    return round(worst, 1)
